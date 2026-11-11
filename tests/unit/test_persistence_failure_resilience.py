"""Regression tests for #7258 transient persistence failures, logging, and HTTP/1.1 client setup."""
import logging
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from postgrest.exceptions import APIError
from supabase._sync.client import create_client as real_create_client

import position_manager as pm_module
import storage as storage_module
from config import settings
from models import AresSignal, Direction, SetupType
from system_one import consumer
from tests.unit.test_option_lifecycle_commit_boundary import make_pm


def _logger_with_client(client, configured=True):
    analytics = storage_module.AnalyticsLogger.__new__(storage_module.AnalyticsLogger)
    analytics.supabase = client
    analytics._option_backend_configured = configured
    return analytics


def test_analytics_logger_configures_http1_client_on_real_supabase(monkeypatch):
    monkeypatch.setattr(storage_module, "create_client", real_create_client)
    analytics = storage_module.AnalyticsLogger()
    session = analytics.supabase.postgrest.session
    try:
        assert isinstance(session, httpx.Client)
        assert session._transport._pool._http2 is False
        assert session.timeout == httpx.Timeout(30.0, connect=10.0)
    finally:
        session.close()


@pytest.mark.parametrize(
    "transient_exc",
    [
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        httpx.ReadTimeout("timed out"),
        ConnectionResetError("connection reset by peer"),
        APIError({"message": "Bad Gateway", "code": 502}),
        APIError({"message": "Service Unavailable", "code": "503"}),
    ],
)
def test_option_rpc_retries_transient_errors_and_succeeds(monkeypatch, transient_exc):
    sleeps = []
    monkeypatch.setattr(storage_module.time, "sleep", sleeps.append)

    client = Mock()
    expected = {"success": True, "revision": 2, "idempotent_skip": True}
    client.rpc.return_value.execute.side_effect = [
        transient_exc,
        transient_exc,
        SimpleNamespace(data=expected),
    ]
    analytics = _logger_with_client(client)

    result = analytics.apply_option_lifecycle_transition("trade-1", 1, "evt-1", {"option_trailing": {}})
    assert result == expected
    assert client.rpc.call_count == 3
    assert len(sleeps) == 2


def test_option_rpc_retries_exhaust_after_three_attempts_and_reraise(monkeypatch):
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)
    client = Mock()
    err = httpx.RemoteProtocolError("stream reset")
    client.rpc.return_value.execute.side_effect = [err, err, err]
    analytics = _logger_with_client(client)

    with pytest.raises(httpx.RemoteProtocolError):
        analytics.apply_option_lifecycle_transition("trade-1", 1, "evt-1", {"option_trailing": {}})
    assert client.rpc.call_count == 3


@pytest.mark.parametrize(
    "non_transient_exc",
    [
        PermissionError("denied"),
        ValueError("bad input"),
        RuntimeError("Option lifecycle transaction did not confirm a commit"),
        APIError({"message": "Revision conflict: expected 1, found 2", "code": "40001"}),
        APIError({"message": "Boolean code", "code": True}),
        APIError({"message": "Non-numeric code", "code": "PGRST301"}),
    ],
)
def test_non_transient_rpc_errors_fail_without_retry(monkeypatch, non_transient_exc):
    sleeps = []
    monkeypatch.setattr(storage_module.time, "sleep", sleeps.append)

    client = Mock()
    client.rpc.return_value.execute.side_effect = non_transient_exc
    analytics = _logger_with_client(client)

    with pytest.raises(type(non_transient_exc)):
        analytics.apply_option_lifecycle_transition("trade-1", 1, "evt-1", {"option_trailing": {}})
    assert client.rpc.call_count == 1
    assert sleeps == []


def test_other_idempotent_option_methods_and_capabilities_retry_transient_errors(monkeypatch):
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)
    client = Mock()
    err = httpx.ReadError("reset")
    client.rpc.return_value.execute.side_effect = [
        err, SimpleNamespace(data={"success": True, "claimed": True}),
        err, SimpleNamespace(data={"success": True, "revision": 2}),
        err, SimpleNamespace(data={"ready": True, "schema_version": "option_premium_protection_v2"}),
        err, SimpleNamespace(data={"ready": True, "schema_version": "one_lot_hold_to_exit_v1"}),
    ]
    analytics = _logger_with_client(client)

    ack = analytics.acknowledge_option_lifecycle_alert("t1", "e1", "SENDING")
    ref = analytics.apply_option_reference_capture("t1", 1, "e2", {"kind": "EXIT"})
    assert ack["claimed"] is True
    assert ref["revision"] == 2
    assert analytics.option_premium_protection_ready() is True
    assert analytics.option_reference_capture_ready() is True

    table_err = httpx.RemoteProtocolError("reset")
    analytics_query = Mock()
    analytics_query.select.return_value.eq.return_value.execute.side_effect = [
        table_err,
        SimpleNamespace(data=[{"market_context": {"option_trailing": {"revision": 3}}}]),
    ]
    active_query = Mock()
    active_query.select.return_value.eq.return_value.execute.return_value = SimpleNamespace(
        data=[{"state": "OPEN", "stop_loss": 25000.0}]
    )
    client.table.side_effect = lambda name: analytics_query if name == "trade_analytics" else active_query
    state = analytics.get_option_lifecycle_state("t1")
    assert state["option_trailing"]["revision"] == 3


@pytest.mark.asyncio
async def test_flush_option_trade_recovers_from_transient_rpc_blip_without_false_alert(monkeypatch):
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)
    pm, trade, sender = make_pm(monkeypatch)
    client = Mock()
    client.rpc.return_value.execute.side_effect = [
        httpx.RemoteProtocolError("Server disconnected"),
        SimpleNamespace(data={"success": True, "revision": 2, "option_trailing": {"revision": 2, "alert_events": {}}}),
    ]
    pm.analytics = _logger_with_client(client)
    pm._option_runtime()
    pm._option_pending[trade["id"]] = [("evt-1", {"option_trailing": {"revision": 1}})]

    await pm._flush_option_trade(trade)
    assert pm._option_pending[trade["id"]] == []
    assert trade["market_context"]["option_trailing"]["revision"] == 2
    assert sender.await_count == 0


@pytest.mark.asyncio
async def test_flush_option_trade_logs_exception_and_reload_failure(monkeypatch, caplog):
    assert hasattr(pm_module, "logger")
    pm, trade, sender = make_pm(monkeypatch)
    pm.analytics.apply_option_lifecycle_transition = Mock(side_effect=ValueError("Revision conflict: expected 1, found 2"))
    pm.analytics.get_option_lifecycle_state = Mock(side_effect=RuntimeError("state select timed out"))
    pm._option_runtime()
    pm._option_pending[trade["id"]] = [("evt-2", {"option_trailing": {"revision": 1}, "update_type": "QUOTE"})]

    with caplog.at_level(logging.WARNING, logger="position_manager"):
        await pm._flush_option_trade(trade)

    assert "Option lifecycle conflict reload failed" in caplog.text
    assert "state select timed out" in caplog.text
    assert "Option lifecycle persistence failed" in caplog.text
    assert "Revision conflict: expected 1, found 2" in caplog.text
    assert sender.await_count == 1
    assert sender.call_args.args[0]["update_type"] == "PERSISTENCE_FAILED"


def test_consumer_run_silences_httpx_info_noise(monkeypatch):
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    prev_httpx = httpx_logger.level
    prev_httpcore = httpcore_logger.level
    try:
        httpx_logger.setLevel(logging.NOTSET)
        httpcore_logger.setLevel(logging.NOTSET)
        monkeypatch.setattr(consumer, "_create_supabase_client", lambda: object())
        monkeypatch.setattr(consumer, "_bootstrap_consumer_state", lambda _sb: {"live_from": "09:15", "max_signal_age_seconds": 300})
        monkeypatch.setattr(consumer, "Thread", lambda *a, **kw: SimpleNamespace(start=lambda: None, join=lambda timeout=None: None))
        monkeypatch.setattr("system_one.watch_consumer.start_watch_workers", lambda *a, **kw: [])
        monkeypatch.setattr(consumer, "_is_trading_session", Mock(side_effect=KeyboardInterrupt()))

        consumer.run()

        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
    finally:
        httpx_logger.setLevel(prev_httpx)
        httpcore_logger.setLevel(prev_httpcore)


def test_all_storage_and_position_clients_configure_http1_and_timeout(monkeypatch):
    monkeypatch.setattr(storage_module, "create_client", real_create_client)
    monkeypatch.setattr(pm_module, "create_client", real_create_client)
    monkeypatch.setattr(settings, "supabase_service_role_key", "test-service-role-key")
    monkeypatch.setattr(pm_module.PositionManager, "_initialize_db", lambda self: None)

    clients = [
        storage_module.get_supabase_client(),
        storage_module.Storage().supabase,
        storage_module.AnalyticsLogger().supabase,
        (pred := storage_module.PredictionLogger(max_workers=1)).supabase,
        (pm := pm_module.PositionManager()).supabase,
    ]
    clients.append(pm.analytics.supabase)
    try:
        for client in clients:
            session = client.postgrest.session
            try:
                assert isinstance(session, httpx.Client)
                assert session._transport._pool._http2 is False
                assert session.timeout == httpx.Timeout(30.0, connect=10.0)
            finally:
                session.close()
    finally:
        pred.shutdown(wait=True)


def test_analytics_log_exit_and_weekly_backfill_retry_transient_errors(monkeypatch):
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(settings, "signal_schema_mode", "greenfield")
    client = Mock()
    err = httpx.RemoteProtocolError("Server disconnected")

    analytics_table = Mock()
    analytics_table.select.return_value.eq.return_value.execute.side_effect = [
        err,
        SimpleNamespace(data=[{
            "entry_price": 25000.0, "direction": "BULLISH", "signal_id": None,
            "setup_type": "TREND_CONTINUATION", "entry_timestamp": "2026-11-11T05:00:00+00:00",
            "score": 1,
        }]),
    ]
    analytics_table.update.return_value.eq.return_value.execute.side_effect = [
        err,
        SimpleNamespace(data=[{"id": "t-exit"}]),
    ]
    client.table.return_value = analytics_table
    analytics = _logger_with_client(client)

    analytics._execute_with_retry = analytics._execute_with_retry  # sanity check helper exists
    # Run synchronous exit helper via missing event loop fallback or direct asyncio
    monkeypatch.setattr(storage_module.asyncio, "get_running_loop", Mock(side_effect=RuntimeError))
    analytics.log_exit("t-exit", 25025.0, "T1_HIT", "2026-11-11T05:10:00+00:00")
    assert analytics_table.select.return_value.eq.return_value.execute.call_count == 2
    assert analytics_table.update.return_value.eq.return_value.execute.call_count == 2

    analytics.option_reference_capture_ready = Mock(return_value=True)
    client.rpc.return_value.execute.side_effect = [
        err,
        SimpleNamespace(data={"success": True, "backfilled": 16}),
    ]
    rows = [{
        "id": f"t-{i}", "contract": {"security_id": str(i)},
        "entry_event_at": "2026-10-26T09:20:00+05:30",
        "exit_event_at": "2026-10-26T09:40:00+05:30",
        "exit_reason": "SL_HIT", "entry": {"price": 100}, "exit": {"price": 110},
    } for i in range(16)]
    result = analytics.apply_option_reference_weekly_backfill(rows)
    assert result["backfilled"] == 16
    assert client.rpc.return_value.execute.call_count == 2


def test_execute_with_retry_releases_lock_during_backoff():
    analytics = _logger_with_client(Mock())
    analytics._supabase_lock = threading.RLock()
    other_acquired = threading.Event()
    sleeping_started = threading.Event()
    calls = {"count": 0}

    def fake_sleep(_delay):
        sleeping_started.set()
        assert other_acquired.wait(timeout=1.0), "lock must be released while sleeping in backoff"

    def contender():
        assert sleeping_started.wait(timeout=1.0)
        if analytics._supabase_lock.acquire(timeout=0.5):
            other_acquired.set()
            analytics._supabase_lock.release()

    def flaky():
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ReadTimeout("blip")
        return "ok"

    t = threading.Thread(target=contender)
    t.start()
    try:
        old_sleep = storage_module.time.sleep
        storage_module.time.sleep = fake_sleep
        try:
            assert analytics._execute_with_retry(flaky, max_attempts=2, base_delay=0.01) == "ok"
        finally:
            storage_module.time.sleep = old_sleep
        assert other_acquired.is_set()
    finally:
        t.join(timeout=1.0)


@pytest.mark.asyncio
async def test_position_manager_persists_recover_from_transient_errors_without_multiplying_entry_rpc(monkeypatch):
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(pm_module.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(settings, "signal_schema_mode", "greenfield")
    monkeypatch.setattr(pm_module.PositionManager, "_initialize_db", lambda self: None)

    err = httpx.RemoteProtocolError("Server disconnected")
    client = Mock()
    monkeypatch.setattr(pm_module, "create_client", lambda *_a, **_kw: client)
    monkeypatch.setattr(pm_module, "AnalyticsLogger", lambda: _logger_with_client(Mock()))
    pm = pm_module.PositionManager()

    # 1) _persist_trade_context recovers transient select + update blips
    ta_table = Mock()
    ta_table.select.return_value.eq.return_value.execute.side_effect = [
        err,
        SimpleNamespace(data=[{"market_context": {}}]),
    ]
    ta_table.update.return_value.eq.return_value.execute.side_effect = [
        err,
        SimpleNamespace(data=[{"id": "trade-pm"}]),
    ]
    # 2) sync_execution recovers transient ml_collection blip
    ml_table = Mock()
    ml_table.update.return_value.eq.return_value.is_.return_value.execute.side_effect = [
        err,
        SimpleNamespace(data=[{"trade_id": "trade-pm"}]),
    ]
    client.table.side_effect = lambda name: ta_table if name == "trade_analytics" else ml_table

    atr_snap = {"state": "T1_HIT", "stop_loss": 25000.0}
    pm._pending_atr_trailing_context["trade-pm"] = atr_snap
    pm._persist_trade_context("trade-pm", atr_snapshot=atr_snap)
    assert "trade-pm" not in pm._pending_atr_trailing_context
    assert ta_table.select.return_value.eq.return_value.execute.call_count == 2
    assert ta_table.update.return_value.eq.return_value.execute.call_count == 2

    sig = AresSignal(
        setup_type=SetupType.TREND_CONTINUATION,
        direction=Direction.BULLISH,
        trigger_price=25000.0,
        entry_zone=(24995.0, 25005.0),
        stop_loss=24980.0,
        target_1=25020.0,
        target_2=25060.0,
        confidence="HIGH",
        reasons=[],
        timestamp=datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc),
        strike_to_trade=25000,
        option_type="CE",
    )
    sig.trade_id = "trade-pm"
    assert await pm.sync_execution(sig, None) is True
    assert ml_table.update.return_value.eq.return_value.is_.return_value.execute.call_count == 2

    # 3) add_trade RPC must serialize via _supabase_lock WITHOUT multiplying outer 3 attempts (3 total, not 9)
    sig.market_context = {"tracking_mode": "SIGNAL_REFERENCE"}
    client.rpc.return_value.execute.side_effect = err
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        await pm.add_trade(sig, 25000.0)
    assert client.rpc.return_value.execute.call_count == 3

    # 4) _record_trade_update active_trades update recovers from transient blip
    monkeypatch.setattr(pm_module, "send_trade_update", AsyncMock())
    active_table = Mock()
    active_table.update.return_value.eq.return_value.execute.side_effect = [
        err,
        SimpleNamespace(data=[{"id": "trade-rec"}]),
    ]
    client.table.side_effect = lambda name: active_table
    trade = {"id": "trade-rec", "state": "T1_HIT", "stop_loss": 25000.0, "market_context": {}}
    await pm._record_trade_update(trade, 25020.0, "T1_HIT", "2026-11-11T10:05:00+00:00")
    pending_task = pm._trade_writes.pop("trade-rec", None)
    if pending_task is not None:
        await pending_task
    assert active_table.update.return_value.eq.return_value.execute.call_count == 2
