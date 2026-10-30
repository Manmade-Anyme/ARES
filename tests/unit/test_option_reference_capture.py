from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import execution.option_reference_capture as capture
from execution.option_reference_capture import _process_row, run_option_reference_capture
from models import AresSignal, Direction, SetupType
from position_manager import PositionManager

IST = timezone(timedelta(hours=5, minutes=30))
CONTRACT = {
    "security_id": "51333", "exchange_segment": "NSE_FNO", "expiry_date": "2026-11-03",
    "strike": 22550, "option_type": "PE", "lot_size": 65,
}


def _reference(entry_at):
    return {
        "formula_version": "one_lot_hold_to_exit_v1", "revision": 1,
        "status": "TRACKING", "reference_lots": 1, "contract": deepcopy(CONTRACT),
        "entry_event_at": entry_at.isoformat(), "entry": None, "milestones": {},
        "exit_event_at": None, "exit_reason": None, "exit": None,
        "premium_points": None, "applied_events": {},
    }


class _Analytics:
    _option_backend_configured = True

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.events = []
        self.fail_apply = False
        self.ready_error = None
        self.list_error = None

    def option_reference_capture_ready(self):
        if self.ready_error:
            raise self.ready_error
        return True

    def list_incomplete_option_reference(self, limit, offset):
        if self.list_error:
            raise self.list_error
        return self.rows[offset:offset + limit]

    def apply_option_reference_capture(self, trade_id, expected_revision, event_id, event):
        if self.fail_apply:
            raise RuntimeError("capture RPC unavailable")
        self.events.append((trade_id, expected_revision, event_id, deepcopy(event)))
        row = next((item for item in self.rows if str(item["id"]) == str(trade_id)), None)
        reference = row["market_context"]["option_reference"] if row else None
        if reference is None:
            raise AssertionError("capture should target an enrolled row")
        if event["kind"] == "ENTRY_CAPTURE":
            reference["entry"] = deepcopy(event["evidence"])
        elif event["kind"] == "MILESTONE":
            reference["milestones"][event["name"]] = deepcopy(event["milestone"])
        elif event["kind"] == "EXIT_MARKER":
            reference["exit_event_at"] = event["event_at"]
            reference["exit_reason"] = event["exit_reason"]
            reference["status"] = "EXIT_PENDING"
        elif event["kind"] == "EXIT_CAPTURE":
            reference["exit"] = deepcopy(event["evidence"])
            reference["exit_event_at"] = event["event_at"]
            reference["exit_reason"] = event["exit_reason"]
            reference["status"] = "CLOSED"
        reference["revision"] += 1
        reference["applied_events"][event_id] = deepcopy(event)
        return {"success": True, "revision": reference["revision"],
                "option_reference": deepcopy(reference)}


@pytest.mark.asyncio
async def test_capture_worker_uses_pinned_recent_quote_and_one_revision_event():
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    now = event_at + timedelta(seconds=2)
    row = {"id": "trade-1", "entry_timestamp": event_at.isoformat(), "exit_timestamp": None,
           "result_state": "OPEN", "market_context": {"option_reference": _reference(event_at)}}
    manager = SimpleNamespace(_context_locks={}, active_trades=[])
    analytics = _Analytics([row])
    fetcher = SimpleNamespace(
        fetch_option_observation=AsyncMock(return_value={
            "price": 140.25, "observed_at": (event_at + timedelta(seconds=1)).isoformat(),
            "source": "dhan_last_trade",
        }),
        fetch_option_minutes=AsyncMock(return_value=[]),
    )

    await _process_row(manager, fetcher, analytics, row, {}, {}, now)

    assert len(analytics.events) == 1
    assert analytics.events[0][1] == 1
    assert analytics.events[0][3] == {
        "kind": "ENTRY_CAPTURE", "event_at": event_at.isoformat(),
        "evidence": {"price": 140.25, "observed_at": (event_at + timedelta(seconds=1)).isoformat(),
                     "source": "dhan_last_trade", "approximate": False},
    }
    fetcher.fetch_option_observation.assert_awaited_once_with(CONTRACT)
    fetcher.fetch_option_minutes.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_worker_persists_queued_spot_milestones_separately():
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    now = event_at + timedelta(minutes=2)
    reference = _reference(event_at)
    row = {"id": "trade-2", "entry_timestamp": event_at.isoformat(), "exit_timestamp": None,
           "result_state": "OPEN", "market_context": {"option_reference": deepcopy(reference)}}
    event = {"kind": "MILESTONE", "name": "T1", "milestone": {
        "event_at": (event_at + timedelta(minutes=1)).isoformat(), "spot_target": 22870,
        "spot_price": None, "source": "completed_candle_touch", "time_basis": "candle_start",
    }}
    trade = {"id": "trade-2", "state": "OPEN", "entry_timestamp": event_at.isoformat(),
             "market_context": {"option_reference": reference}}
    manager = SimpleNamespace(_context_locks={}, active_trades=[trade],
                              _pending_option_reference_events={"trade-2": [event]})
    analytics = _Analytics([row])
    fetcher = SimpleNamespace(fetch_option_observation=AsyncMock(return_value=None),
                              fetch_option_minutes=AsyncMock(return_value=[]))

    await _process_row(manager, fetcher, analytics, row, {}, {}, now)

    assert [item[3]["kind"] for item in analytics.events] == ["MILESTONE"]
    assert analytics.events[0][3]["name"] == "T1"
    assert manager._pending_option_reference_events == {}
    assert trade["market_context"]["option_reference"]["milestones"]["T1"]["spot_target"] == 22870
    fetcher.fetch_option_observation.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_worker_recovers_durable_milestone_after_restart():
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    reference = _reference(event_at)
    reference["entry"] = {
        "price": 140, "observed_at": event_at.isoformat(),
        "source": "dhan_last_trade", "approximate": False,
    }
    marker = {
        "event_at": (event_at + timedelta(minutes=1)).isoformat(),
        "spot_target": 22870, "spot_price": None,
        "source": "completed_candle_touch", "time_basis": "candle_start",
    }
    row = {
        "id": "trade-recovered", "entry_timestamp": event_at.isoformat(),
        "exit_timestamp": None, "result_state": "OPEN",
        "market_context": {
            "option_reference": reference,
            "option_reference_milestones": {"T1": marker},
        },
    }
    manager = SimpleNamespace(_context_locks={}, active_trades=[])
    analytics = _Analytics([row])
    fetcher = SimpleNamespace(fetch_option_observation=AsyncMock(return_value=None),
                              fetch_option_minutes=AsyncMock(return_value=[]))

    await _process_row(manager, fetcher, analytics, row, {}, {}, event_at + timedelta(minutes=2))

    assert [item[3] for item in analytics.events] == [{
        "kind": "MILESTONE", "name": "T1", "milestone": marker,
    }]
    assert row["market_context"]["option_reference"]["milestones"]["T1"] == marker


@pytest.mark.asyncio
async def test_capture_worker_does_nothing_when_feature_flag_is_off(monkeypatch):
    monkeypatch.setattr("execution.option_reference_capture.settings.option_reference_accounting_enabled", False)
    manager = SimpleNamespace(active_trades=[])
    analytics = _Analytics()
    fetcher = SimpleNamespace()

    result = await run_option_reference_capture(manager, fetcher, analytics)

    assert result == {"rows_seen": 0, "events_applied": 0}
    assert analytics.events == []


def test_option_reference_pin_needs_no_entry_limit_or_option_stop():
    sizing = {
        "security_id": "51333", "expiry_date": "2026-11-03", "strike": 22550,
        "option_type": "PE", "lot_size": 65,
    }
    pin = PositionManager._validated_option_reference_contract(
        sizing, "2026-10-30T09:20:00+05:30",
    )

    assert pin == CONTRACT
    assert PositionManager._validated_option_reference_contract(
        {**sizing, "expiry_date": "2026-10-29"}, "2026-10-30T09:20:00+05:30",
    ) is None


@pytest.mark.asyncio
async def test_reference_enrollment_is_independent_of_option_atr(monkeypatch):
    monkeypatch.setattr("position_manager.settings.option_atr_enabled", False)
    monkeypatch.setattr("position_manager.settings.option_reference_accounting_enabled", True)
    monkeypatch.setattr("position_manager.settings.supabase_service_role_key", "test-service-key")
    monkeypatch.setattr("position_manager.settings.signal_schema_mode", "bridge")
    analytics = SimpleNamespace(
        option_reference_capture_ready=MagicMock(return_value=True),
        option_reference_accounting_ready=MagicMock(return_value=True),
    )
    pm = PositionManager.__new__(PositionManager)
    pm.analytics = analytics
    pm.active_trades = []
    pm._pending_reference_registrations = {}
    pm.supabase = SimpleNamespace(rpc=MagicMock(side_effect=lambda name, payload: SimpleNamespace(
        execute=lambda: SimpleNamespace(data=payload["p_trade_id"]),
    )))
    signal = AresSignal(
        setup_type=SetupType.FAILED_BREAKOUT, direction=Direction.BEARISH,
        trigger_price=22800.0, entry_zone=(22795.0, 22805.0),
        stop_loss=22825.0, target_1=22770.0, target_2=22740.0,
        confidence="HIGH", reasons=["test"],
        timestamp=datetime(2026, 10, 30, 9, 20, tzinfo=IST),
        strike_to_trade=22550, option_type="PE",
    )
    signal.market_context = {
        "tracking_mode": "SIGNAL_REFERENCE",
        "options_sizing": {
            "security_id": CONTRACT["security_id"],
            "expiry_date": CONTRACT["expiry_date"],
            "strike": CONTRACT["strike"],
            "option_type": CONTRACT["option_type"],
            "lot_size": CONTRACT["lot_size"],
        },
    }

    trade_id, status = await pm.add_trade(signal, 22800.0)

    assert status == "BOUND"
    assert analytics.option_reference_capture_ready.call_count == 1
    analytics.option_reference_accounting_ready.assert_not_called()
    market_context = pm.active_trades[0]["market_context"]
    assert market_context["option_reference"]["contract"] == CONTRACT
    assert "option_trailing" not in market_context
    assert pm.supabase.rpc.call_args.args[1]["p_trade_id"] == trade_id


def test_option_reference_milestone_uses_durable_mirror_without_changing_spot_marker():
    manager = PositionManager.__new__(PositionManager)
    legacy = {"touched_at": "2026-10-30T09:21:00+05:30", "target_spot": 22870}
    trade = {
        "id": "trade-mirror", "direction": "BULLISH", "entry_price": 22800,
        "market_context": {
            "target_milestones": {"t2": deepcopy(legacy)},
            "option_reference": _reference(datetime(2026, 10, 30, 9, 20, tzinfo=IST)),
        },
    }

    assert manager._queue_option_reference_milestone(
        trade, "T1", "2026-10-30T09:21:00+05:30", 22870, None,
        source="completed_candle_touch", time_basis="candle_start",
    )

    assert trade["market_context"]["target_milestones"]["t2"] == legacy
    assert trade["market_context"]["option_reference_milestones"]["T1"] == {
        "event_at": "2026-10-30T09:21:00+05:30",
        "spot_target": 22870.0, "spot_price": None,
        "source": "completed_candle_touch", "time_basis": "candle_start",
    }


def test_capture_helpers_reject_invalid_values_and_remove_only_matching_events():
    event = {"kind": "MILESTONE", "name": "T1", "milestone": {"x": 1}}
    other = {"kind": "MILESTONE", "name": "T2", "milestone": {"x": 2}}
    manager = SimpleNamespace(_pending_option_reference_events={"trade": [event, other]})

    assert capture._iso("not a timestamp") is None
    assert capture._reference({"market_context": {"option_reference": {"formula_version": "v0"}}}) is None
    assert capture._has_leg({}, "entry") is False
    assert capture._has_pending_exit({"exit_event_at": None, "exit_reason": None}) is False
    assert capture._row_from_active({"id": "old", "market_context": {}}) is None
    active = {"id": "active", "entry_timestamp": "2026-10-30T09:20:00+05:30",
              "market_context": {"option_reference": _reference(
                  datetime(2026, 10, 30, 9, 20, tzinfo=IST),
              )}}
    assert capture._row_from_active(active)["id"] == "active"
    assert capture._pending_for(manager, "trade") == [event, other]
    capture._remove_pending(manager, "trade", event)
    assert manager._pending_option_reference_events == {"trade": [other]}
    capture._remove_pending(manager, "trade", other)
    assert manager._pending_option_reference_events == {}
    assert capture._durable_milestone_events(
        {"option_reference_milestones": {"T1": {"event_at": "x", "spot_target": 1}}},
        {"milestones": {}},
    ) == []


@pytest.mark.asyncio
async def test_quote_and_minute_fetch_failures_are_cached_and_deferred():
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    fetcher = SimpleNamespace(
        fetch_option_observation=AsyncMock(side_effect=RuntimeError("quote unavailable")),
        fetch_option_minutes=AsyncMock(side_effect=RuntimeError("history unavailable")),
    )
    quote_cache, minute_cache = {}, {}

    assert await capture._quote(fetcher, CONTRACT, capture._contract_key(CONTRACT), quote_cache) is None
    assert await capture._quote(fetcher, CONTRACT, capture._contract_key(CONTRACT), quote_cache) is None
    assert await capture._minutes(fetcher, CONTRACT, event_at, minute_cache) == []
    assert await capture._minutes(fetcher, CONTRACT, event_at, minute_cache) == []
    assert fetcher.fetch_option_observation.await_count == 1
    assert fetcher.fetch_option_minutes.await_count == 1


@pytest.mark.asyncio
async def test_price_evidence_defers_forming_bar_then_uses_completed_history(monkeypatch):
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    fetcher = SimpleNamespace(
        fetch_option_observation=AsyncMock(return_value=None),
        fetch_option_minutes=AsyncMock(return_value=[{"complete": True}]),
    )
    quote_cache, minute_cache = {}, {}
    now = event_at + timedelta(seconds=2)

    assert await capture._price_evidence(fetcher, CONTRACT, event_at, now, quote_cache, minute_cache) is None
    fetcher.fetch_option_minutes.assert_not_awaited()
    monkeypatch.setattr(capture, "_evidence", lambda event, *, quote, bars, now: (
        {"price": 141, "approximate": True} if bars else None
    ))
    result = await capture._price_evidence(
        fetcher, CONTRACT, event_at, event_at + timedelta(minutes=2), quote_cache, minute_cache,
    )

    assert result == {"price": 141, "approximate": True}
    fetcher.fetch_option_minutes.assert_awaited_once()


@pytest.mark.asyncio
async def test_minute_fallback_uses_post_fetch_validation_cutoff(monkeypatch):
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    pass_now = event_at + timedelta(minutes=1, seconds=5)
    request_started = pass_now + timedelta(seconds=1)
    fetched_at = request_started + timedelta(seconds=1)
    minute = event_at.replace(second=0, microsecond=0)
    bar = {
        "timestamp": minute.isoformat(), "sampled_at": request_started.isoformat(),
        "complete": True, "open": 140, "high": 142, "low": 139, "close": 141,
        "volume": 10,
    }
    fetcher = SimpleNamespace(
        fetch_option_minutes=AsyncMock(return_value=[bar]),
    )
    monkeypatch.setattr(capture, "datetime", SimpleNamespace(
        now=MagicMock(return_value=fetched_at),
    ))

    evidence = await capture._price_evidence(
        fetcher, CONTRACT, event_at, pass_now, {}, {},
    )

    assert evidence == {
        "price": 141.0, "observed_at": (minute + timedelta(minutes=1)).isoformat(),
        "source": "completed_1m_close", "approximate": True,
    }
    assert bar["sampled_at"] == request_started.isoformat()
    fetcher.fetch_option_minutes.assert_awaited_once_with(CONTRACT, event_at)


def test_apply_sync_requires_positive_revision_and_updates_without_existing_lock_map():
    manager = SimpleNamespace()
    analytics = SimpleNamespace(apply_option_reference_capture=AsyncMock())
    event = {"kind": "MILESTONE"}
    reference = {"revision": 0}

    with pytest.raises(ValueError, match="invalid revision"):
        capture._apply_sync(manager, analytics, "trade", reference, event)
    assert "trade" in manager._context_locks
    reference["revision"] = 1
    analytics.apply_option_reference_capture = lambda *args: args

    assert capture._apply_sync(manager, analytics, "trade", reference, event) == (
        "trade", 1, capture._event_id(event), event,
    )
    assert "trade" in manager._context_locks


@pytest.mark.asyncio
async def test_process_row_rejects_unenrolled_or_incomplete_rows():
    now = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    manager = SimpleNamespace(active_trades=[])
    fetcher = SimpleNamespace()
    analytics = _Analytics()
    invalid_rows = [
        {},
        {"id": "bad", "market_context": {}},
        {"id": "bad", "market_context": {"option_reference": {
            "formula_version": capture.FORMULA_VERSION,
        }}},
        {"id": "bad", "market_context": {"option_reference": {
            "formula_version": capture.FORMULA_VERSION, "contract": CONTRACT,
            "entry_event_at": None,
        }}},
    ]

    for row in invalid_rows:
        assert await _process_row(manager, fetcher, analytics, row, {}, {}, now) == 0
    assert analytics.events == []


@pytest.mark.asyncio
async def test_persist_event_leaves_snapshot_when_rpc_returns_no_reference():
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    trade = {"id": "trade-no-snapshot", "market_context": {
        "option_reference": _reference(event_at),
    }}
    reference = trade["market_context"]["option_reference"]
    analytics = SimpleNamespace(apply_option_reference_capture=lambda *args: {"success": True})
    manager = SimpleNamespace(_context_locks={})

    result = await capture._persist_event(
        manager, analytics, trade, reference, {"kind": "MILESTONE"},
    )

    assert result == {"success": True}
    assert trade["market_context"]["option_reference"] is reference


@pytest.mark.asyncio
async def test_process_row_deduplicates_captured_milestone_and_defers_rpc_failure():
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    reference = _reference(event_at)
    marker = {"event_at": event_at.isoformat(), "spot_target": 22870,
              "spot_price": None, "source": "spot_observation", "time_basis": "spot_observation"}
    reference["milestones"]["T1"] = marker
    row = {"id": "trade-duplicate", "entry_timestamp": event_at.isoformat(),
           "exit_timestamp": None, "result_state": "OPEN",
           "market_context": {"option_reference": reference}}
    event = {"kind": "MILESTONE", "name": "T1", "milestone": deepcopy(marker)}
    manager = SimpleNamespace(active_trades=[], _pending_option_reference_events={"trade-duplicate": [event]})
    analytics = _Analytics([row])
    fetcher = SimpleNamespace()

    assert await _process_row(manager, fetcher, analytics, row, {}, {}, event_at) == 0
    assert manager._pending_option_reference_events == {}
    assert analytics.events == []

    reference = _reference(event_at)
    row["market_context"]["option_reference"] = reference
    manager._pending_option_reference_events["trade-duplicate"] = [event]
    analytics.fail_apply = True
    assert await _process_row(manager, fetcher, analytics, row, {}, {}, event_at) == 0
    assert manager._pending_option_reference_events["trade-duplicate"] == [event]


@pytest.mark.asyncio
async def test_process_row_skips_unknown_and_duplicate_milestone_events():
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    reference = _reference(event_at)
    reference["entry"] = {"price": 140, "observed_at": event_at.isoformat(),
                          "source": "dhan_last_trade", "approximate": False}
    marker = {"event_at": (event_at + timedelta(minutes=1)).isoformat(),
              "spot_target": 22870, "spot_price": None,
              "source": "spot_observation", "time_basis": "spot_observation"}
    event = {"kind": "MILESTONE", "name": "T1", "milestone": marker}
    row = {"id": "trade-deduplicated", "entry_timestamp": event_at.isoformat(),
           "exit_timestamp": None, "result_state": "OPEN",
           "market_context": {"option_reference": reference}}
    manager = SimpleNamespace(active_trades=[], _pending_option_reference_events={
        "trade-deduplicated": [{"kind": "IGNORED"}, event, deepcopy(event)],
    })
    analytics = _Analytics([row])

    assert await _process_row(
        manager, SimpleNamespace(), analytics, row, {}, {}, event_at + timedelta(minutes=2),
    ) == 1
    assert [item[3]["kind"] for item in analytics.events] == ["MILESTONE"]
    assert manager._pending_option_reference_events == {"trade-deduplicated": [{"kind": "IGNORED"}]}


@pytest.mark.asyncio
async def test_process_row_defers_entry_and_exit_capture_exceptions(monkeypatch):
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    exit_at = event_at + timedelta(minutes=10)
    reference = _reference(event_at)
    row = {"id": "trade-deferred", "entry_timestamp": event_at.isoformat(),
           "exit_timestamp": exit_at.isoformat(), "result_state": "SL_HIT",
           "market_context": {"option_reference": reference}}
    analytics = _Analytics([row])
    calls = 0

    async def fail_price(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("price history unavailable")

    monkeypatch.setattr(capture, "_price_evidence", fail_price)
    result = await _process_row(
        SimpleNamespace(active_trades=[]), SimpleNamespace(), analytics, row, {}, {},
        exit_at + timedelta(minutes=2),
    )

    assert result == 1  # The terminal marker persists; both price lookups defer.
    assert calls == 2
    assert [item[3]["kind"] for item in analytics.events] == ["EXIT_MARKER"]


@pytest.mark.asyncio
async def test_process_row_records_closed_exit_marker_and_capture(monkeypatch):
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    exit_at = event_at + timedelta(minutes=10)
    reference = _reference(event_at)
    reference["entry"] = {"price": 140, "observed_at": event_at.isoformat(),
                          "source": "dhan_last_trade", "approximate": False}
    row = {"id": "trade-closed", "entry_timestamp": event_at.isoformat(),
           "exit_timestamp": exit_at.isoformat(), "result_state": "SL_HIT",
           "market_context": {"option_reference": reference}}
    manager = SimpleNamespace(active_trades=[])
    analytics = _Analytics([row])
    fetcher = SimpleNamespace()
    evidence = {"price": 132, "observed_at": exit_at.isoformat(),
                "source": "dhan_last_trade", "approximate": False}
    monkeypatch.setattr(capture, "_price_evidence", AsyncMock(return_value=evidence))

    await _process_row(manager, fetcher, analytics, row, {}, {}, exit_at + timedelta(seconds=1))

    assert [item[3]["kind"] for item in analytics.events] == ["EXIT_MARKER", "EXIT_CAPTURE"]
    assert row["market_context"]["option_reference"]["exit"] == evidence


@pytest.mark.asyncio
async def test_process_row_recovers_existing_exit_marker_without_recapturing_leg(monkeypatch):
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    exit_at = event_at + timedelta(minutes=10)
    reference = _reference(event_at)
    reference["entry"] = {"price": 140, "observed_at": event_at.isoformat(),
                          "source": "dhan_last_trade", "approximate": False}
    reference["exit_event_at"] = exit_at.isoformat()
    reference["exit_reason"] = "SL_HIT"
    row = {"id": "trade-pending-exit", "entry_timestamp": event_at.isoformat(),
           "exit_timestamp": exit_at.isoformat(), "result_state": "SL_HIT",
           "market_context": {"option_reference": reference}}
    manager = SimpleNamespace(active_trades=[])
    analytics = _Analytics([row])
    evidence = {"price": 132, "observed_at": exit_at.isoformat(),
                "source": "dhan_last_trade", "approximate": False}
    price_evidence = AsyncMock(return_value=evidence)
    monkeypatch.setattr(capture, "_price_evidence", price_evidence)

    await _process_row(manager, SimpleNamespace(), analytics, row, {}, {}, exit_at + timedelta(seconds=1))

    assert [item[3]["kind"] for item in analytics.events] == ["EXIT_CAPTURE"]
    assert price_evidence.await_count == 1

    reference["exit"] = evidence
    analytics.events.clear()
    price_evidence.reset_mock()
    await _process_row(manager, SimpleNamespace(), analytics, row, {}, {}, exit_at + timedelta(seconds=1))
    assert analytics.events == []
    price_evidence.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_capture_feature_and_backend_gates(monkeypatch):
    monkeypatch.setattr("execution.option_reference_capture.settings.option_reference_accounting_enabled", True)
    manager = SimpleNamespace(active_trades=[])
    fetcher = object()
    analytics = _Analytics()

    analytics._option_backend_configured = False
    assert await run_option_reference_capture(manager, fetcher, analytics) == {
        "rows_seen": 0, "events_applied": 0,
    }
    analytics._option_backend_configured = True
    analytics.ready_error = RuntimeError("probe unavailable")
    assert await run_option_reference_capture(manager, fetcher, analytics) == {
        "rows_seen": 0, "events_applied": 0,
    }
    analytics.ready_error = None
    analytics.option_reference_capture_ready = lambda: False
    assert await run_option_reference_capture(manager, fetcher, analytics) == {
        "rows_seen": 0, "events_applied": 0,
    }


@pytest.mark.asyncio
async def test_run_capture_handles_scan_error_row_errors_and_offset_cycle(monkeypatch):
    monkeypatch.setattr("execution.option_reference_capture.settings.option_reference_accounting_enabled", True)
    manager = SimpleNamespace(active_trades=[], _option_reference_recovery_offset=3)
    analytics = _Analytics()
    analytics.list_error = RuntimeError("scan unavailable")
    assert await run_option_reference_capture(manager, object(), analytics) == {
        "rows_seen": 0, "events_applied": 0,
    }
    assert manager._option_reference_recovery_offset == 0

    analytics.list_error = None
    row = {"id": "invalid-row", "market_context": {}}
    analytics.rows = [row]
    async def fail_row(*args, **kwargs):
        raise RuntimeError("row failure")
    monkeypatch.setattr(capture, "_process_row", fail_row)

    first = await run_option_reference_capture(manager, object(), analytics, limit=1, max_rows=1)
    assert first == {"rows_seen": 1, "events_applied": 0}
    assert manager._option_reference_recovery_offset == 1
    second = await run_option_reference_capture(manager, object(), analytics, limit=1, max_rows=1)
    assert second == {"rows_seen": 0, "events_applied": 0}
    assert manager._option_reference_recovery_offset == 0


@pytest.mark.asyncio
async def test_run_capture_merges_active_trade_with_queued_milestone(monkeypatch):
    monkeypatch.setattr("execution.option_reference_capture.settings.option_reference_accounting_enabled", True)
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    reference = _reference(event_at)
    reference["entry"] = {"price": 140, "observed_at": event_at.isoformat(),
                          "source": "dhan_last_trade", "approximate": False}
    marker = {"event_at": (event_at + timedelta(minutes=1)).isoformat(),
              "spot_target": 22870, "spot_price": None,
              "source": "spot_observation", "time_basis": "spot_observation"}
    event = {"kind": "MILESTONE", "name": "T1", "milestone": marker}
    trade = {"id": "trade-active", "entry_timestamp": event_at.isoformat(),
             "result_state": "OPEN", "market_context": {"option_reference": reference}}
    manager = SimpleNamespace(active_trades=[trade], _context_locks={},
                              _pending_option_reference_events={"trade-active": [event]})
    analytics = _Analytics([{
        "id": "trade-active", "entry_timestamp": event_at.isoformat(),
        "exit_timestamp": None, "result_state": "OPEN",
        "market_context": {"option_reference": deepcopy(reference)},
    }])
    analytics.list_incomplete_option_reference = lambda limit, offset: []

    result = await run_option_reference_capture(manager, SimpleNamespace(), analytics)

    assert result == {"rows_seen": 1, "events_applied": 1}
    assert trade["market_context"]["option_reference"]["milestones"]["T1"] == marker


@pytest.mark.asyncio
async def test_process_row_defers_exit_when_price_evidence_is_unavailable(monkeypatch):
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    exit_at = event_at + timedelta(minutes=10)
    reference = _reference(event_at)
    reference["entry"] = {"price": 140, "observed_at": event_at.isoformat(),
                          "source": "dhan_last_trade", "approximate": False}
    reference["exit_event_at"] = exit_at.isoformat()
    reference["exit_reason"] = "SL_HIT"
    row = {"id": "trade-exit-unavailable", "entry_timestamp": event_at.isoformat(),
           "exit_timestamp": exit_at.isoformat(), "result_state": "SL_HIT",
           "market_context": {"option_reference": reference}}
    analytics = _Analytics([row])
    price_evidence = AsyncMock(return_value=None)
    monkeypatch.setattr(capture, "_price_evidence", price_evidence)

    assert await _process_row(
        SimpleNamespace(active_trades=[]), SimpleNamespace(), analytics, row, {}, {},
        exit_at + timedelta(minutes=2),
    ) == 0
    assert analytics.events == []
    price_evidence.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_capture_skips_unenrolled_and_unqueued_active_trades(monkeypatch):
    monkeypatch.setattr("execution.option_reference_capture.settings.option_reference_accounting_enabled", True)
    event_at = datetime(2026, 10, 30, 9, 20, tzinfo=IST)
    manager = SimpleNamespace(active_trades=[
        {"id": "unenrolled", "market_context": {}},
        {"id": "enrolled", "market_context": {
            "option_reference": _reference(event_at),
        }},
    ])
    analytics = _Analytics()
    analytics.list_incomplete_option_reference = lambda limit, offset: []

    assert await run_option_reference_capture(manager, SimpleNamespace(), analytics) == {
        "rows_seen": 0, "events_applied": 0,
    }
    assert analytics.events == []
