"""Comprehensive unit tests covering all remaining uncovered diff lines in PR #150.

Targets:
- alerts.py (line 800)
- execution/reference_prices.py (lines 16, 17, 22, 25, 26, 34, 38, 46, 54, 55, 57, 79, 92, 96, 115, 118, 129, 138, 141)
- execution/reference_recovery.py (lines 35, 46, 50, 82, 85, 95, 115, 117, 119, 130, 151, 163, 168-175, 202, 208, 209)
- fetchers/price_fetcher.py (lines 79, 306, 315, 316, 329-331, 366, 372, 405, 408-410, 415, 417, 418, 420)
- main.py (lines 895-899, 901, 902)
- position_manager.py (lines 109, 110, 112, 113, 335, 336, 706, 769, 789, 809, 1092, 1104-1106, 1136-1144, 1157, 1160, 1163, 1172, 1174, 1175, 1177, 1183, 1185, 1202, 1207, 1208, 1212, 1216, 1221, 1222, 1230, 1236, 1244, 1418, 1590-1592)
"""
import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import alerts
from config import settings
from execution.reference_prices import (
    _minute_close,
    _nearest_observation,
    _price,
    _timestamp,
    normalize_option_minutes,
    option_target_evidence,
    spot_exit_evidence,
)
from execution.reference_recovery import (
    _closed_trade,
    _complete_after_correction,
    _merge_minutes,
    _option,
    run_reference_recovery,
)
from fetchers.price_fetcher import PriceFetcher, ReferencePriceDeferred
import main
from models import AresSignal, Direction, OHLCVCandle, SetupType
from position_manager import PositionManager
from tests.unit.test_manm176_review_persistence import MemoryDatabase
from tests.unit.test_option_atr_lifecycle_comprehensive import (
    _make_pm,
    _make_signal,
    _mock_trade_from_signal,
)

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_DATE = date(2026, 10, 23)
START = datetime(2026, 10, 23, 10, 30, tzinfo=IST)


# ============================================================================
# 1. alerts.py (Line 800)
# ============================================================================

@pytest.mark.asyncio
async def test_alerts_t2_touched_entry_boundary_label():
    """alerts.py:800: marker time_basis == 'entry_boundary'."""
    trade = {
        "signal_id": "TEST_T2_BOUND",
        "display_id": "9901",
        "setup_type": "ORB",
        "direction": "BULLISH",
        "state": "T1_HIT",
        "entry_price": 22400.0,
        "target_1": 22500.0,
        "target_2": 22600.0,
        "stop_loss": 22300.0,
        "market_context": {
            "target_milestones": {
                "t2": {
                    "touched_at": "2026-10-23T10:30:00Z",
                    "time_basis": "entry_boundary",
                    "target_spot": 22600.0,
                    "spot_points_from_entry": 200.0,
                }
            }
        },
    }
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.status_code = 204
        mock_post.return_value.raise_for_status = Mock()
        await alerts.send_trade_update(trade, 22600.0, "T2_TOUCHED")

        mock_post.assert_awaited_once()
        payload = mock_post.call_args[1]["json"]
        desc = payload["embeds"][0]["description"]
        assert "(entry boundary; exact touch time unknown)" in desc


# ============================================================================
# 2. execution/reference_prices.py
# ============================================================================

def test_reference_prices_timestamp_exceptions():
    """Lines 16-17: _timestamp exception handling."""
    assert _timestamp("not-a-valid-timestamp-format") is None
    assert _timestamp(object()) is None


def test_reference_prices_price_boolean_and_exceptions():
    """Lines 22, 25-26: _price bool check and float conversion exception."""
    assert _price(True) is None
    assert _price(False) is None
    assert _price("not_a_number") is None
    assert _price(None) is None
    assert _price(float("nan")) is None
    assert _price(-10.0) is None
    assert _price(105.5) == 105.5


def test_reference_prices_normalize_option_minutes_edge_cases():
    """Lines 34, 38, 46, 54-55, 57: normalize_option_minutes validations."""
    sampled_str = "2026-10-23T10:35:00+05:30"
    valid_time = START.isoformat()

    # Line 34: sampled is None or data is not a dict
    assert normalize_option_minutes("not_a_dict", SESSION_DATE, sampled_str) == []
    assert normalize_option_minutes({}, SESSION_DATE, "invalid-sampled-timestamp") == []

    # Line 38: non-list/tuple series
    bad_data = {
        "timestamp": [valid_time],
        "open": "not_a_list",
        "high": [110.0],
        "low": [90.0],
        "close": [105.0],
        "volume": [1000],
    }
    assert normalize_option_minutes(bad_data, SESSION_DATE, sampled_str) == []

    # Line 46: date mismatch, second != 0, out of session, future timestamp
    mismatched_data = {
        "timestamp": [
            "2026-10-24T10:30:00+05:30",  # Different date
            "2026-10-23T10:30:15+05:30",  # second != 0
            "2026-10-23T08:00:00+05:30",  # before 09:15
            "2026-10-23T11:00:00+05:30",  # at > sampled (sampled is 10:35)
        ],
        "open": [100.0, 100.0, 100.0, 100.0],
        "high": [110.0, 110.0, 110.0, 110.0],
        "low": [90.0, 90.0, 90.0, 90.0],
        "close": [105.0, 105.0, 105.0, 105.0],
        "volume": [100, 100, 100, 100],
    }
    assert normalize_option_minutes(mismatched_data, SESSION_DATE, sampled_str) == []

    # Lines 54-55, 57: invalid volume and invalid price values
    invalid_row_data = {
        "timestamp": [valid_time, START.replace(minute=31).isoformat()],
        "open": [100.0, "bad_open"],
        "high": [110.0, 110.0],
        "low": [90.0, 90.0],
        "close": [105.0, 105.0],
        "volume": ["invalid_volume_str", 100],  # row 0 invalid volume, row 1 invalid price
    }
    rows = normalize_option_minutes(invalid_row_data, SESSION_DATE, sampled_str)
    assert len(rows) == 2
    assert rows[0].get("open") is None
    assert rows[0]["complete"] is False
    assert rows[1].get("open") is None
    assert rows[1]["complete"] is False


def test_reference_prices_minute_close_invalid_ohlc():
    """Line 79: _minute_close skipping row where any OHLC price is None."""
    minute = START
    observed_at = START + timedelta(minutes=5)
    candles = [
        {
            "timestamp": minute.isoformat(),
            "sampled_at": (minute + timedelta(minutes=1)).isoformat(),
            "complete": True,
            "open": "bad_price",
            "high": 110.0,
            "low": 90.0,
            "close": 105.0,
        }
    ]
    assert _minute_close(candles, minute, observed_at) is None


def test_reference_prices_nearest_observation_edge_cases():
    """Lines 92, 96: _nearest_observation skipping non-dict and invalid source."""
    event = START
    observed_at = START + timedelta(minutes=1)
    observations = [
        "not_a_dict",
        {"observed_at": event.isoformat(), "price": 100.0, "source": "   "},
        {"observed_at": event.isoformat(), "price": 100.0, "source": 123},
    ]
    assert _nearest_observation(observations, event, observed_at) is None


def test_reference_prices_option_target_evidence_edge_cases():
    """Lines 115, 118, 129: option_target_evidence guards."""
    # Line 115: invalid observed_at or invalid event_at
    assert option_target_evidence("2026-10-23T10:30:00Z", observed_at="invalid-date") is None
    assert option_target_evidence("invalid-event-at", observed_at="2026-10-23T10:35:00Z") is None

    # Line 118: event > now
    now = datetime(2026, 10, 23, 10, 30, tzinfo=IST)
    future_event = datetime(2026, 10, 23, 10, 35, tzinfo=IST)
    assert option_target_evidence(future_event, observed_at=now) is None

    # Line 129: wick-only touch with out-of-session source_minute
    out_of_session = datetime(2026, 10, 23, 8, 30, tzinfo=IST)
    assert option_target_evidence(None, source_minute=out_of_session, observed_at=now) is None


def test_reference_prices_spot_exit_evidence_edge_cases():
    """Lines 138, 141: spot_exit_evidence guards."""
    now = datetime(2026, 10, 23, 10, 35, tzinfo=IST)

    # Line 138: event > now or event.second != 0
    future_exit = datetime(2026, 10, 23, 10, 40, tzinfo=IST)
    assert spot_exit_evidence(future_exit, observed_at=now) is None
    non_zero_second = datetime(2026, 10, 23, 10, 30, 15, tzinfo=IST)
    assert spot_exit_evidence(non_zero_second, observed_at=now) is None

    # Line 141: out of session minute (e.g. exit at 09:15:00 -> minute is 09:14:00)
    boundary_exit = datetime(2026, 10, 23, 9, 15, 0, tzinfo=IST)
    assert spot_exit_evidence(boundary_exit, observed_at=now) is None


# ============================================================================
# 3. execution/reference_recovery.py
# ============================================================================

def test_reference_recovery_helpers_edge_cases():
    """Lines 35, 46, 50, 82, 85, 95: helper functions edge cases."""
    # Line 35: _option with non-dict trade
    assert _option("not_a_dict") is None

    # Line 46: _closed_trade with non-dict active_trade
    assert _closed_trade({"active_trade": "not_a_dict"}) is None

    # Line 50: _closed_trade with non-dict option_trailing
    assert _closed_trade({"active_trade": {}, "option_trailing": "not_a_dict"}) is None

    # Line 82: _merge_minutes skipping non-dict rows
    assert _merge_minutes(["not_a_dict"], []) == []

    # Line 85: _merge_minutes skipping invalid timestamps
    assert _merge_minutes([{"timestamp": "bad", "sampled_at": "bad"}], []) == []

    # Line 95: _complete_after_correction without original_option_entry
    assert _complete_after_correction({}, {"original_option_entry": None}, {}) is False


@pytest.mark.asyncio
async def test_reference_recovery_parameter_validation_and_query_failure():
    """Lines 115, 117, 119, 130: validation and query error handling."""
    pm = _make_pm()
    fetcher = SimpleNamespace()
    analytics = SimpleNamespace()

    # Line 115: limit validation
    with pytest.raises(ValueError, match="limit must be between 1 and 500"):
        await run_reference_recovery(pm, fetcher, analytics, limit=0)

    # Line 117: max_pages validation
    with pytest.raises(ValueError, match="max_pages must be positive"):
        await run_reference_recovery(pm, fetcher, analytics, max_pages=0)

    # Line 119: offset validation
    with pytest.raises(ValueError, match="offset must be a non-negative integer"):
        await run_reference_recovery(pm, fetcher, analytics, offset=-1)

    # Line 130: page not a list
    analytics.list_incomplete_option_accounting = Mock(return_value="not_a_list")
    res = await run_reference_recovery(pm, fetcher, analytics)
    assert res == {"rows_seen": 0, "corrected": 0}


@pytest.mark.asyncio
async def test_reference_recovery_legs_and_quote_requests():
    """Lines 151, 163, 168-175, 202, 208, 209: leg iteration, quote requests, retry and truncate."""
    pm = _make_pm()
    now = datetime.now(timezone.utc)
    exact_recent = now - timedelta(seconds=2)

    trade = _mock_trade_from_signal(_make_signal(), trade_id="quote-trade-1")
    opt = trade["market_context"]["option_trailing"]
    opt.update(
        policy_version="option_atr_v1",
        accounting_formula_version="one_lot_reference_v1",
        contract={
            "security_id": "9999",
            "exchange_segment": "NSE_FNO",
            "expiry_date": "2026-10-30",
            "strike": 24000.0,
            "option_type": "CE",
        },
    )
    opt["accounting"] = {
        "legs": {
            "NON_DICT_LEG": "invalid_leg_content",  # Line 151
            "T1": {
                "source_minute": None,
                "event_at": exact_recent.isoformat(),
                "option_price": None,
                "spot_price": 24000.0,
            },
        }
    }
    trade_closed = deepcopy(trade)
    trade_closed["id"] = "closed-trade"
    trade_closed["state"] = "CLOSED"
    pm.active_trades = [trade, trade_closed]
    pm._reference_price_evidence = {
        "option_quotes": {"9999": [{"price": 100.0}] * 105}  # Line 202: truncation test
    }

    analytics = SimpleNamespace(list_incomplete_option_accounting=Mock(return_value=[]))

    # Test quote_requests success (lines 170-171)
    fetcher_success = SimpleNamespace(
        fetch_option_observation=AsyncMock(return_value={"price": 150.0, "observed_at": now.isoformat()}),
        fetch_option_minutes=AsyncMock(return_value=[]),
        fetch_spot_minutes=AsyncMock(return_value=[]),
    )
    await run_reference_recovery(pm, fetcher_success, analytics)
    assert len(pm._reference_price_evidence["option_quotes"]["9999"]) <= 100

    # Test quote_requests ReferencePriceDeferred (lines 172-173)
    fetcher_deferred = SimpleNamespace(
        fetch_option_observation=AsyncMock(side_effect=ReferencePriceDeferred("cooldown")),
        fetch_option_minutes=AsyncMock(return_value=[]),
        fetch_spot_minutes=AsyncMock(return_value=[]),
    )
    await run_reference_recovery(pm, fetcher_deferred, analytics)

    # Test quote_requests Exception logging (lines 174-175) and enrich_reference_accounting exception (lines 208-209)
    fetcher_err = SimpleNamespace(
        fetch_option_observation=AsyncMock(side_effect=RuntimeError("quote error")),
        fetch_option_minutes=AsyncMock(return_value=[]),
        fetch_spot_minutes=AsyncMock(return_value=[]),
    )
    pm.enrich_reference_accounting = AsyncMock(side_effect=RuntimeError("enrichment failed"))
    await run_reference_recovery(pm, fetcher_err, analytics)


# ============================================================================
# 4. fetchers/price_fetcher.py
# ============================================================================

@pytest.mark.asyncio
async def test_price_fetcher_fetch_latest_spot_success():
    """Line 79: fetch_latest_spot calls _protective_request."""
    fetcher = PriceFetcher()
    fetcher.dhan = Mock()
    fetcher.dhan.ticker_data.return_value = {
        "status": "success",
        "data": {settings.exchange_segment: {str(settings.security_id): {"last_price": 24123.45}}},
    }
    spot = await fetcher.fetch_latest_spot()
    assert spot == 24123.45


def test_price_fetcher_reference_contract_exceptions():
    """Lines 306, 315-316: _reference_contract validation exceptions."""
    # Line 306: contract not a dict
    with pytest.raises(ValueError, match="Reference price requires a pinned option contract"):
        PriceFetcher._reference_contract("not_a_dict", "2026-10-23T10:30:00Z")

    # Lines 315-316: invalid expiry or strike format
    bad_contract = {
        "security_id": "12345",
        "exchange_segment": "NSE_FNO",
        "expiry_date": "invalid-expiry",
        "strike": "invalid-strike",
    }
    with pytest.raises(ValueError, match="Reference price requires a complete option pin"):
        PriceFetcher._reference_contract(bad_contract, "2026-10-23T10:30:00Z")


@pytest.mark.asyncio
async def test_price_fetcher_protective_request_executor_base_exception():
    """Lines 329-331: _protective_request catching BaseException from executor."""
    fetcher = PriceFetcher()
    loop = asyncio.get_running_loop()

    with patch.object(loop, "run_in_executor", side_effect=KeyboardInterrupt("cancelled")):
        with pytest.raises(KeyboardInterrupt):
            await fetcher._protective_request(lambda: None)
    assert getattr(fetcher, "_protective_active", 0) == 0


@pytest.mark.asyncio
async def test_price_fetcher_fetch_option_minutes_edge_cases():
    """Lines 366, 372: fetch_option_minutes future event and API error."""
    contract = {
        "security_id": "12345",
        "exchange_segment": "NSE_FNO",
        "expiry_date": "2026-10-30",
        "strike": 24000.0,
        "option_type": "CE",
    }
    fetcher = PriceFetcher()
    fetcher.dhan = Mock()

    # Line 366: event > now
    future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    res = await fetcher.fetch_option_minutes(contract, future_time)
    assert res == []

    # Line 372: response status != 'success'
    fetcher.dhan.intraday_minute_data.return_value = {"status": "failure"}
    past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="Pinned option minute history request failed"):
        await fetcher.fetch_option_minutes(contract, past_time)


@pytest.mark.asyncio
async def test_price_fetcher_fetch_option_observation_edge_cases():
    """Lines 405, 408-410, 415, 417-418, 420: fetch_option_observation error cases."""
    contract = {
        "security_id": "12345",
        "exchange_segment": "NSE_FNO",
        "expiry_date": "2026-10-30",
        "strike": 24000.0,
        "option_type": "CE",
    }
    fetcher = PriceFetcher()
    fetcher.dhan = Mock()

    def reset_cooldown():
        fetcher._reference_next_request = 0.0
        fetcher._protective_last_request = float("-inf")
        fetcher._protective_active = 0

    # Line 405: response status != 'success'
    reset_cooldown()
    fetcher.dhan.quote_data.return_value = {"status": "error"}
    with pytest.raises(ValueError, match="Pinned option quote request failed"):
        await fetcher.fetch_option_observation(contract)

    # Lines 408-410: payload data['status'] != 'success'
    reset_cooldown()
    fetcher.dhan.quote_data.return_value = {
        "status": "success",
        "data": {"status": "error", "data": {}},
    }
    with pytest.raises(ValueError, match="Pinned option quote payload failed"):
        await fetcher.fetch_option_observation(contract)

    # Line 415: last_price is bool
    reset_cooldown()
    fetcher.dhan.quote_data.return_value = {
        "status": "success",
        "data": {"NSE_FNO": {"12345": {"last_price": True}}},
    }
    with pytest.raises(ValueError, match="Pinned option quote is missing or invalid"):
        await fetcher.fetch_option_observation(contract)

    # Lines 417-418: last_price missing or invalid type
    reset_cooldown()
    fetcher.dhan.quote_data.return_value = {
        "status": "success",
        "data": {"NSE_FNO": {"12345": {"last_price": "invalid_price_str"}}},
    }
    with pytest.raises(ValueError, match="Pinned option quote is missing or invalid"):
        await fetcher.fetch_option_observation(contract)

    # Line 420: price <= 0
    reset_cooldown()
    fetcher.dhan.quote_data.return_value = {
        "status": "success",
        "data": {"NSE_FNO": {"12345": {"last_price": -10.0}}},
    }
    with pytest.raises(ValueError, match="Pinned option quote is missing or invalid"):
        await fetcher.fetch_option_observation(contract)

    # Line 410: payload data['data'] unwrapped on success
    reset_cooldown()
    now_str = datetime.now(IST).strftime("%d/%m/%Y %H:%M:%S")
    fetcher.dhan.quote_data.return_value = {
        "status": "success",
        "data": {
            "status": "success",
            "data": {"NSE_FNO": {"12345": {"last_price": 125.0, "last_trade_time": now_str}}},
        },
    }
    obs = await fetcher.fetch_option_observation(contract)
    assert obs["price"] == 125.0


# ============================================================================
# 5. main.py (Lines 895-899, 901, 902)
# ============================================================================

@pytest.mark.asyncio
async def test_main_shutdown_cancels_recovery_task_and_handles_drain_error(monkeypatch):
    """Lines 895-899, 901, 902: main.run shutdown drain and recovery task cancellation."""
    from tests.unit.test_session_close_drain import candle

    first = candle(minute=28)
    final = candle(minute=29)
    times = [datetime(2026, 10, 19, 15, 29), datetime(2026, 10, 19, 15, 30)]

    engine = MagicMock()
    engine.tick.side_effect = [None, None]
    engine.candle_buffer = []

    price = MagicMock()
    price.fetch_latest_candle = AsyncMock(side_effect=[first, final])
    price.fetch_latest_spot = AsyncMock(return_value=24000.0)
    price.fetch_previous_day_ohlc = AsyncMock(return_value=(24100.0, 23900.0))

    oi = MagicMock()
    oi.get_nearest_expiry = AsyncMock(return_value="2099-01-01")
    oi.fetch_chain = AsyncMock(return_value=(None, []))

    positions = MagicMock()
    positions.update_trades = AsyncMock(return_value=[])

    async def dummy_recovery():
        await asyncio.sleep(100)

    recovery_task = asyncio.create_task(dummy_recovery())
    positions._reference_recovery_task = recovery_task

    positions.flush_pending_trade_updates = AsyncMock(side_effect=RuntimeError("flush timeout"))

    dispatcher = MagicMock()
    dispatcher.on_snapshot = AsyncMock()
    dispatcher.on_spot = AsyncMock()
    dispatcher.dispatch = AsyncMock()

    collector = MagicMock()
    collector.check_table_exists.return_value = False
    collector.snapshot = AsyncMock()
    collector.stats = {"total_snapshots": 0, "signals_recorded": 0}

    feed = MagicMock(is_active=False)
    clock = MagicMock()
    clock.now.side_effect = times
    outbox = MagicMock()
    outbox.close = AsyncMock()

    for name, instance in {
        "AresEngine": engine,
        "PriceFetcher": price,
        "OIFetcher": oi,
        "LevelFetcher": MagicMock(),
        "Storage": MagicMock(),
        "PositionManager": positions,
        "SignalDispatcher": dispatcher,
        "OrderManager": MagicMock(),
        "MLCollector": collector,
        "TickFeed": feed,
        "SignalPredictor": MagicMock(),
        "PredictionLogger": MagicMock(),
    }.items():
        monkeypatch.setattr(main, name, MagicMock(return_value=instance))

    for name, val in {
        "settings": MagicMock(poll_interval_seconds=1, tick_exit_check_interval_seconds=1, candle_buffer_size=10),
        "datetime": clock,
        "_session_now": MagicMock(return_value=times[0]),
        "load_dhan_credentials_from_supabase": MagicMock(),
        "is_expiry_day_from_api": AsyncMock(return_value=False),
        "send_startup_alert": AsyncMock(),
        "send_error_alert": AsyncMock(),
        "send_discord": AsyncMock(),
        "process_options_calculation": AsyncMock(),
        "dispatch_oi_wall_watch_alerts": AsyncMock(),
        "initialize_oi_watch_outbox": AsyncMock(return_value=outbox),
        "print_banner": MagicMock(),
        "format_signal_console": MagicMock(),
    }.items():
        monkeypatch.setattr(main, name, val)

    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())

    await main.run()
    assert recovery_task.cancelled()


# ============================================================================
# 6. position_manager.py
# ============================================================================

@pytest.mark.asyncio
async def test_position_manager_flush_pending_trade_updates_timeout_and_unpersisted():
    """Lines 109, 110, 112, 113: flush_pending_trade_updates timeout and unpersisted paths."""
    pm = _make_pm()

    # Lines 109-110: pending tasks timeout
    pm._trade_writes = {}
    pm._pending_target_milestones = {"trade-1": {"touched_at": "..."}}
    async def slow():
        await asyncio.sleep(10)
    slow_task = asyncio.create_task(slow())
    def fake_queue(trade_id, write_fn):
        pm._trade_writes[trade_id] = slow_task
    with patch.object(pm, "_queue_trade_write", side_effect=fake_queue):
        res1 = await pm.flush_pending_trade_updates(timeout=0.01)
        slow_task.cancel()
        try:
            await slow_task
        except asyncio.CancelledError:
            pass
        assert res1 is False

    # Lines 112-113: milestones remain unpersisted
    pm._trade_writes = {}
    pm._pending_target_milestones = {"trade-2": {"touched_at": "..."}}
    res2 = await pm.flush_pending_trade_updates(timeout=0.0)
    assert res2 is False


@pytest.mark.asyncio
async def test_position_manager_add_trade_ready_check_exception(monkeypatch):
    """Lines 335-336: add_trade capability check exception."""
    monkeypatch.setattr("position_manager.logger", logging.getLogger("position_manager"), raising=False)
    database = MemoryDatabase()
    monkeypatch.setattr(settings, "option_atr_enabled", True)
    monkeypatch.setattr(settings, "option_atr_approved_version", "option_atr_v1")
    monkeypatch.setattr(settings, "supabase_service_role_key", "test-key")
    signal = _make_signal()
    signal.metadata = {
        "options_sizing": {
            "contract": {"security_id": "123", "exchange_segment": "NSE_FNO", "strike": 24000, "option_type": "CE"},
            "option_sl": 100.0,
            "entry_limit": 110.0,
        }
    }
    with patch("position_manager.create_client", return_value=database), patch("position_manager.AnalyticsLogger") as analytics_cls:
        analytics_cls.return_value.option_reference_accounting_ready.side_effect = RuntimeError("db error")
        pm = PositionManager()
        await pm.add_trade(signal, 22500.0)
        assert pm.active_trades[-1]["market_context"].get("option_atr_unavailable") is not None


@pytest.mark.asyncio
async def test_position_manager_update_trades_reference_legs():
    """Lines 706, 769, 789: reference accounting in BE stop, double-touch T1/T2, and initial SL."""
    # Line 706: STOPPED_OUT_AT_BE with reference accounting (Bullish runner drops back to BE entry)
    pm = _make_pm()
    trade_be = _mock_trade_from_signal(_make_signal(), trade_id="trade-be")
    trade_be["direction"] = "BULLISH"
    trade_be["state"] = "T1_HIT"
    trade_be["entry_price"] = 22400.0
    trade_be["target_2"] = 22600.0
    trade_be["stop_loss"] = 22400.0
    opt_be = trade_be["market_context"]["option_trailing"]
    opt_be.update(policy_version="option_atr_v1", accounting_formula_version="one_lot_reference_v1")
    pm.active_trades = [trade_be]
    events = await pm.update_trades(22390.0, candle_high=22420.0, candle_low=22380.0, candle_timestamp=START)
    assert any(ev[1] == "STOPPED_OUT_AT_BE" for ev in events)
    assert "EXIT" in trade_be["market_context"]["option_trailing"]["accounting"]["legs"]

    # Line 769: Target 1 and Target 2 hit in same bar without touching guard
    pm2 = _make_pm()
    trade_both = _mock_trade_from_signal(_make_signal(), trade_id="trade-both")
    trade_both["direction"] = "BULLISH"
    trade_both["state"] = "OPEN"
    trade_both["entry_price"] = 22400.0
    trade_both["target_1"] = 22500.0
    trade_both["target_2"] = 22600.0
    opt_both = trade_both["market_context"]["option_trailing"]
    opt_both.update(policy_version="option_atr_v1", accounting_formula_version="one_lot_reference_v1")
    pm2.active_trades = [trade_both]
    # High touches 22650 (> T1 and T2), low is 22450 (> entry_price 22400, guard not touched)
    events2 = await pm2.update_trades(22650.0, candle_high=22650.0, candle_low=22450.0, candle_timestamp=START)
    assert any(ev[1] == "T2_HIT" for ev in events2)
    assert "T2" in trade_both["market_context"]["option_trailing"]["accounting"]["legs"]

    # Line 789: Stop hit with reference accounting
    pm3 = _make_pm()
    trade_sl = _mock_trade_from_signal(_make_signal(), trade_id="trade-sl")
    trade_sl["direction"] = "BULLISH"
    trade_sl["state"] = "OPEN"
    trade_sl["entry_price"] = 22400.0
    trade_sl["target_1"] = 22500.0
    trade_sl["stop_loss"] = 22300.0
    opt_sl = trade_sl["market_context"]["option_trailing"]
    opt_sl.update(policy_version="option_atr_v1", accounting_formula_version="one_lot_reference_v1")
    pm3.active_trades = [trade_sl]
    events3 = await pm3.update_trades(22250.0, candle_high=22350.0, candle_low=22250.0, candle_timestamp=START)
    assert any(ev[1] == "SL_HIT" for ev in events3)
    assert "EXIT" in trade_sl["market_context"]["option_trailing"]["accounting"]["legs"]


@pytest.mark.asyncio
async def test_position_manager_record_t2_touch_pre_entry():
    """Line 809: _record_t2_touch returns False when spot observation event_ts < entry_timestamp."""
    pm = _make_pm()
    trade = _mock_trade_from_signal(_make_signal(), trade_id="trade-t2-pre")
    trade["market_context"]["legacy_t2_tracking_approved"] = True
    trade["entry_timestamp"] = "2026-10-23T10:30:00Z"
    trade["target_2"] = 22600.0
    pre_entry_ts = "2026-10-23T10:29:00Z"
    touched = await pm._record_t2_touch(trade, 22650.0, 22650.0, pre_entry_ts, None, 22650.0)
    assert touched is False


def test_position_manager_merge_reference_evidence_conflict_exception():
    """Line 1092: _merge_reference_evidence raises RuntimeError on conflicting values."""
    pm = _make_pm()
    committed = {"legs": {"T1": {"spot_price": 22500.0}}}
    observed = {"legs": {"T1": {"spot_price": 22600.0}}}
    with pytest.raises(RuntimeError, match="Conflicting committed reference evidence"):
        pm._merge_reference_evidence(committed, observed)


def test_position_manager_capture_reference_leg_spot_approximate():
    """Lines 1104-1106: _capture_reference_leg when option_close and spot_price both present."""
    pm = _make_pm()
    trade = _mock_trade_from_signal(_make_signal(), trade_id="leg-approx")
    opt = trade["market_context"]["option_trailing"]
    opt.update(policy_version="option_atr_v1", accounting_formula_version="one_lot_reference_v1")
    event_at = START.isoformat()
    pm._capture_reference_leg(trade, "EXIT", spot_price=22500.0, event_at=event_at, option_close=125.0)
    leg = opt["accounting"]["legs"]["EXIT"]
    assert leg["spot_source"] == "completed_1m_close"
    assert leg["spot_observed_at"] == event_at
    assert leg["spot_approximate"] is True


@pytest.mark.asyncio
async def test_position_manager_enrich_reference_accounting_updates_open_trade():
    """Lines 1136-1144: enrich_reference_accounting persists newly enriched trade."""
    pm = _make_pm()
    trade = _mock_trade_from_signal(_make_signal(), trade_id="enrich-trade")
    trade["state"] = "OPEN"
    opt = trade["market_context"]["option_trailing"]
    opt.update(
        policy_version="option_atr_v1",
        accounting_formula_version="one_lot_reference_v1",
        contract={"security_id": "8888", "exchange_segment": "NSE_FNO", "strike": 24000, "option_type": "CE"},
    )
    opt["accounting"] = {
        "legs": {
            "T1": {"event_at": START.isoformat(), "spot_price": 22500.0, "option_price": None}
        }
    }
    trade_closed = deepcopy(trade)
    trade_closed["id"] = "closed-trade"
    trade_closed["state"] = "CLOSED"
    pm.active_trades = [trade, trade_closed]
    pm._reference_price_evidence = {
        "option_quotes": {
            "8888": [{"observed_at": START.isoformat(), "price": 142.0, "source": "quote_source"}]
        }
    }

    with patch.object(pm, "_record_option_transition", new_callable=AsyncMock) as mock_transition:
        await pm.enrich_reference_accounting()
        mock_transition.assert_awaited_once()


@pytest.mark.asyncio
async def test_position_manager_resolve_reference_ambiguity_all_branches():
    """Lines 1157, 1160, 1163, 1172, 1174-1175, 1177, 1183, 1185, 1202, 1207-1208, 1212, 1216, 1221-1222, 1230, 1236, 1244."""
    pm = _make_pm()
    trade = _mock_trade_from_signal(_make_signal(), trade_id="ambig-trade")
    trade["state"] = "T1_HIT"
    trade["direction"] = "BULLISH"
    trade["entry_price"] = 22400.0
    trade["target_1"] = 22500.0
    trade["target_2"] = 22600.0
    opt = trade["market_context"]["option_trailing"]
    opt.update(
        policy_version="option_atr_v1",
        accounting_formula_version="one_lot_reference_v1",
        ambiguous_index_touch={"source_minute": START.isoformat(), "kind": "T1_ENTRY"},
    )
    pm.active_trades = [trade]

    # Line 1157: trade state != 'T1_HIT'
    trade["state"] = "OPEN"
    assert await pm.resolve_reference_ambiguity("ambig-trade", [], complete=True) is False
    trade["state"] = "T1_HIT"

    # Line 1160: accounting_formula_version != 'one_lot_reference_v1'
    opt["accounting_formula_version"] = "other"
    assert await pm.resolve_reference_ambiguity("ambig-trade", [], complete=True) is False
    opt["accounting_formula_version"] = "one_lot_reference_v1"

    # Line 1163: missing ambiguity or source_minute
    opt["ambiguous_index_touch"] = None
    assert await pm.resolve_reference_ambiguity("ambig-trade", [], complete=True) is False
    opt["ambiguous_index_touch"] = {"source_minute": START.isoformat(), "kind": "T1_ENTRY"}

    # Line 1172: invalid tick in minute (e.g. tick out of range or price <= 0 or empty source)
    bad_ticks = [{"observed_at": (START - timedelta(seconds=10)).isoformat(), "price": 22500, "source": "src"}]
    assert await pm.resolve_reference_ambiguity("ambig-trade", bad_ticks, complete=True) is False

    # Lines 1174-1175: exception parsing tick
    corrupt_ticks = [{"observed_at": START.isoformat(), "price": "bad_price"}]
    assert await pm.resolve_reference_ambiguity("ambig-trade", corrupt_ticks, complete=True) is False

    # Line 1177: unsorted ticks
    unsorted = [
        {"observed_at": (START + timedelta(seconds=20)).isoformat(), "price": 22500, "source": "src"},
        {"observed_at": (START + timedelta(seconds=10)).isoformat(), "price": 22400, "source": "src"},
    ]
    assert await pm.resolve_reference_ambiguity("ambig-trade", unsorted, complete=True) is False

    # Line 1183: ticks do not touch target_1 (target is 22500, ticks only reach 22450)
    no_target = [
        {"observed_at": (START + timedelta(seconds=10)).isoformat(), "price": 22420, "source": "src"},
        {"observed_at": (START + timedelta(seconds=20)).isoformat(), "price": 22400, "source": "src"},
    ]
    assert await pm.resolve_reference_ambiguity("ambig-trade", no_target, complete=True) is False

    # Line 1185: ticks do not touch entry_price (entry is 22400, ticks lowest is 22450)
    no_entry = [
        {"observed_at": (START + timedelta(seconds=10)).isoformat(), "price": 22510, "source": "src"},
        {"observed_at": (START + timedelta(seconds=20)).isoformat(), "price": 22450, "source": "src"},
    ]
    assert await pm.resolve_reference_ambiguity("ambig-trade", no_entry, complete=True) is False

    # Line 1202: decision is None and not t1_seen
    trade["target_1"] = 22700.0
    trade["target_2"] = 22600.0
    opt["t1_at"] = START.isoformat()
    opt["ambiguous_index_touch"]["event_at"] = START.isoformat()
    opt["ambiguous_index_touch"]["kind"] = "T2_ENTRY"
    ticks_no_seen = [
        {"observed_at": (START + timedelta(seconds=10)).isoformat(), "price": 22400, "source": "src"},
        {"observed_at": (START + timedelta(seconds=20)).isoformat(), "price": 22600, "source": "src"},
    ]
    assert await pm.resolve_reference_ambiguity("ambig-trade", ticks_no_seen, complete=True) is False

    # Restore trade targets and kind for subsequent tests
    trade["target_1"] = 22500.0
    trade["target_2"] = 22600.0
    opt["ambiguous_index_touch"]["kind"] = "T1_ENTRY"
    opt["t1_at"] = START.isoformat()
    opt["ambiguous_index_touch"]["event_at"] = START.isoformat()
    opt["ambiguous_index_touch"]["subsequent_index_observations"] = []

    # Valid ticks for T1_ENTRY where decision is None (entry touched when t1_seen is false; T1 touched, T2 not touched)
    valid_ticks_decision_none = [
        {"observed_at": (START + timedelta(seconds=10)).isoformat(), "price": 22400, "source": "src"},
        {"observed_at": (START + timedelta(seconds=20)).isoformat(), "price": 22510, "source": "src"},
    ]

    # Lines 1207-1208, 1212: subsequent observation event_at <= end
    opt["pending_index_observations"] = [
        {"event_at": (START + timedelta(seconds=30)).isoformat()}  # <= end of START minute
    ]
    assert await pm.resolve_reference_ambiguity("ambig-trade", valid_ticks_decision_none, complete=True) is False

    # Line 1216: duplicate timestamps in subsequent
    opt["pending_index_observations"] = [
        {"event_at": (START + timedelta(minutes=2)).isoformat(), "tag": "1"},
        {"event_at": (START + timedelta(minutes=2)).isoformat(), "tag": "2"},
    ]
    assert await pm.resolve_reference_ambiguity("ambig-trade", valid_ticks_decision_none, complete=True) is False

    # Lines 1221-1222: corrupted timestamp in subsequent
    opt["pending_index_observations"] = [{"event_at": "invalid_timestamp"}]
    assert await pm.resolve_reference_ambiguity("ambig-trade", valid_ticks_decision_none, complete=True) is False

    # Line 1230: later_spot with high or low None
    opt["pending_index_observations"] = [
        {"event_at": (START + timedelta(minutes=2)).isoformat(), "high": None, "low": None}
    ]
    assert await pm.resolve_reference_ambiguity("ambig-trade", valid_ticks_decision_none, complete=True) is False

    # Line 1236: touches both T2 and guard but candle_timestamp is None
    opt["pending_index_observations"] = [
        {
            "event_at": (START + timedelta(minutes=2)).isoformat(),
            "high": 22650.0,
            "low": 22350.0,
            "candle_timestamp": None,
        }
    ]
    assert await pm.resolve_reference_ambiguity("ambig-trade", valid_ticks_decision_none, complete=True) is False

    # Line 1244: touches neither T2 nor guard
    opt["pending_index_observations"] = [
        {
            "event_at": (START + timedelta(minutes=2)).isoformat(),
            "high": 22450.0,
            "low": 22420.0,
            "candle_timestamp": (START + timedelta(minutes=1)).isoformat(),
        }
    ]
    assert await pm.resolve_reference_ambiguity("ambig-trade", valid_ticks_decision_none, complete=True) is False


@pytest.mark.asyncio
async def test_position_manager_flush_option_trade_t2_after_exit_conflict(monkeypatch):
    """Lines 1418, 1590-1592: reconciliation exception when committed T2 follows observed exit."""
    monkeypatch.setattr("position_manager.logger", logging.getLogger("position_manager"), raising=False)
    pm = _make_pm()
    trade = _mock_trade_from_signal(_make_signal(), trade_id="reconcile-trade")
    opt = trade["market_context"]["option_trailing"]
    opt.update(
        revision=1,
        highest_milestone=1,
        policy_version="option_atr_v1",
        accounting_formula_version="one_lot_reference_v1",
    )

    queued_payload = {
        "option_trailing": deepcopy(opt),
        "active_state": "CLOSED",
        "exit_reason": "SL_HIT",
        "exit_timestamp": "2026-10-23T10:30:00Z",
    }
    pm._option_pending = {"reconcile-trade": [("evt-1", queued_payload)]}

    pm.analytics.apply_option_lifecycle_transition = Mock(side_effect=RuntimeError("revision mismatch"))

    persisted_state = {
        "option_trailing": {
            "revision": 2,
            "phase": "OPEN",
            "highest_milestone": 2,
            "t2_at": "2026-10-23T10:35:00Z",
            "accounting_formula_version": "one_lot_reference_v1",
            "accounting": {"legs": {}},
        }
    }
    pm.analytics.get_option_lifecycle_state = Mock(return_value=persisted_state)

    await pm._flush_option_trade(trade)


@pytest.mark.asyncio
async def test_position_manager_flush_option_trade_committed_conflict_logged(monkeypatch):
    """Lines 1590-1592: Committed reference evidence conflicts with newer observation logged."""
    monkeypatch.setattr("position_manager.logger", logging.getLogger("position_manager"), raising=False)
    pm = _make_pm()
    trade = _mock_trade_from_signal(_make_signal(), trade_id="conflict-trade")
    opt = trade["market_context"]["option_trailing"]
    opt.update(
        revision=1,
        policy_version="option_atr_v1",
        accounting_formula_version="one_lot_reference_v1",
        accounting={"legs": {"T1": {"spot_price": 22500.0}}},
    )
    queued_payload = {"option_trailing": deepcopy(opt)}
    pm._option_pending = {"conflict-trade": [("evt-1", queued_payload)]}

    pm.analytics.apply_option_lifecycle_transition = Mock(return_value={
        "success": True,
        "revision": 2,
        "option_trailing": {
            "accounting": {"legs": {"T1": {"spot_price": 22600.0}}}
        }
    })

    await pm._flush_option_trade(trade)

