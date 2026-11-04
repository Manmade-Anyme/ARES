"""Regressions for the independent option-premium stop lifecycle."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from models import OptionTrailingContext, OptionTrailingContract
from options_candles import OptionCandle
from position_manager import PositionManager
from tests.unit.test_option_lifecycle_commit_boundary import Ledger
from alerts import render_option_lifecycle_event

IST = timezone(timedelta(hours=5, minutes=30))


def _trade(*, phase="PRE_T1", entry=167.20, stop=161.05, option_type="CE"):
    ctx = OptionTrailingContext(
        policy_version="option_premium_protection_v2",
        contract=OptionTrailingContract("39961", "NSE_FNO", "2026-11-09", 23950, option_type),
        phase=phase,
        highest_milestone=1 if phase == "POST_T1" else 0,
        booked_fraction=0.5 if phase == "POST_T1" else 0.0,
        remaining_fraction=0.5 if phase == "POST_T1" else 1.0,
        original_option_sl=stop,
        original_option_entry=entry,
        locked_option_sl=entry if phase == "POST_T1" else stop,
        protection_mode="BREAK_EVEN_COST" if phase == "POST_T1" else "INITIAL_OPTION_SL",
        index_guard_stop=23909.90,
        t1_at=datetime(2026, 11, 4, 10, 6, tzinfo=IST).isoformat() if phase == "POST_T1" else None,
        atr_eligible=False,
    ).to_dict()
    return {
        "id": "v2-trade", "display_id": "V2", "direction": "BULLISH",
        "entry_price": 23909.90, "stop_loss": 23896.20,
        "target_1": 23928.20, "target_2": 23963.20,
        "state": "T1_HIT" if phase == "POST_T1" else "OPEN",
        "entry_timestamp": "2026-11-04T03:00:00+00:00",
        "market_context": {"tracking_mode": "SIGNAL_REFERENCE", "option_trailing": ctx},
    }


def _manager(trade):
    pm = object.__new__(PositionManager)
    pm.is_initialized = True
    pm.active_trades = [trade]
    pm._trade_writes = {}
    pm._context_locks = {}
    pm.analytics = Ledger()
    pm._option_available_until = defaultdict(lambda: datetime(2099, 1, 1, tzinfo=IST))
    pm._option_available_until[trade["id"]] = datetime(2099, 1, 1, tzinfo=IST)
    return pm


def _quote(price, minute, *, option_type="CE", security_id="39961"):
    at = datetime(2026, 11, 4, 9, minute, tzinfo=IST)
    return {
        "contract": {
            "security_id": security_id, "exchange_segment": "NSE_FNO",
            "expiry": "2026-11-09", "strike": 23950, "option_type": option_type,
        },
        "price": price, "observed_at": at.isoformat(), "received_at": at.isoformat(),
        "source": "dhan_last_trade",
    }


@pytest.mark.asyncio
async def test_pre_t1_option_quote_at_initial_stop_closes_in_premium_domain():
    trade = _trade()
    pm = _manager(trade)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events([], {}, current_time=datetime(2026, 11, 4, 9, 20, tzinfo=IST),
                                                       option_quotes=[_quote(161.05, 20)])
    assert ("v2-trade", "SL_HIT") in events
    opt = trade["market_context"]["option_trailing"]
    assert trade["state"] == "CLOSED"
    assert opt["exit"]["stop_domain"] == "OPTION_PREMIUM"
    assert opt["exit"]["reason"] == "SL_HIT"


@pytest.mark.asyncio
async def test_post_t1_cost_stop_equality_is_break_even_and_zero_runner_points():
    trade = _trade(phase="POST_T1")
    pm = _manager(trade)
    quote = _quote(167.20, 21)
    quote["observed_at"] = "2026-11-04T10:21:00+05:30"
    quote["received_at"] = quote["observed_at"]
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events([], {}, current_time=datetime(2026, 11, 4, 10, 21, tzinfo=IST),
                                                       option_quotes=[quote])
    assert ("v2-trade", "STOPPED_OUT_AT_BE") in events
    opt = trade["market_context"]["option_trailing"]
    assert opt["exit"]["reason"] == "STOPPED_OUT_AT_BE"
    assert opt["exit"]["option_close"] == pytest.approx(167.20)
    assert opt["exit"]["runner_option_points"] == pytest.approx(0.0)
    assert opt["highest_milestone"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_accounting", [True, False])
async def test_same_batch_t2_and_premium_stop_quote_retain_ambiguity_and_route_protection(with_accounting):
    trade = _trade(phase="POST_T1")
    opt = trade["market_context"]["option_trailing"]
    opt["execution_status"] = "CONFIRMED_PAPER_FILL"
    if with_accounting:
        opt["accounting_formula_version"] = "one_lot_reference_v1"
    pm = _manager(trade)
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    tick = {"price": trade["target_2"], "exchange_at": at.isoformat(),
            "received_at": at.isoformat(), "source": "dhan_tick"}
    quote = _quote(166.0, 21)
    quote.update(observed_at=at.isoformat(), received_at=at.isoformat())

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events(
            [], {}, current_time=at, index_ticks=[tick], option_quotes=[quote])

    assert ("v2-trade", "STOPPED_OUT_AT_BE") in events
    assert trade["state"] == "T1_HIT"
    assert opt["highest_milestone"] == 1
    assert opt.get("exit") is None
    ambiguity = opt["ambiguous_index_touch"]
    assert ambiguity["kind"] == "SPOT_OPTION_EXIT_TIE"
    assert ambiguity["touches_t2"] is True
    assert ambiguity["protective_option_exit"]["option_close"] == pytest.approx(166.0)
    assert ambiguity["protective_option_exit"]["source"] == "dhan_last_trade"
    assert opt["execution_route_pending"] is True
    assert opt["execution_route_reason"] == "STOPPED_OUT_AT_BE"


@pytest.mark.asyncio
async def test_earlier_queued_t2_is_replayed_before_premium_exit_finalizes_reference():
    trade = _trade(phase="POST_T1")
    opt = trade["market_context"]["option_trailing"]
    opt.update(atr_enabled=True, accounting_formula_version="one_lot_reference_v1")
    pm = _manager(trade)
    pm._option_available_until = {}
    t2_at = datetime(2026, 11, 4, 10, 20, 30, tzinfo=IST)
    quote_at = datetime(2026, 11, 4, 10, 21, 0, tzinfo=IST)
    tick = {"price": trade["target_2"], "exchange_at": t2_at.isoformat(),
            "received_at": t2_at.isoformat(), "source": "dhan_tick"}
    quote = _quote(166.0, 21)
    quote.update(observed_at=quote_at.isoformat(), received_at=(quote_at + timedelta(seconds=1)).isoformat())

    start = datetime(2026, 11, 4, 9, 20, tzinfo=IST)
    history = [OptionCandle(start + timedelta(minutes=5 * i), start + timedelta(minutes=5 * (i + 1)),
                            170.0, 171.0, 169.0, 170.0, 1, "39961") for i in range(13)]

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events(
            [], {}, current_time=quote_at + timedelta(seconds=2), index_ticks=[tick], option_quotes=[quote])

    assert ("v2-trade", "STOPPED_OUT_AT_BE") in events  # execution protection is routed immediately
    assert trade["state"] == "T1_HIT"
    assert opt["highest_milestone"] == 1
    assert opt.get("exit") is None
    assert opt["deferred_option_exit"]["event_at"] == quote_at.isoformat()
    assert opt["pending_index_observations"][0]["touches_t2"] is True

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        recovery_events = await pm.update_option_market_events(
            [], {"39961": history}, current_time=start + timedelta(minutes=65, seconds=2))

    assert ("v2-trade", "T2_HIT") in recovery_events
    assert ("v2-trade", "STOPPED_OUT_AT_BE") in recovery_events
    assert trade["state"] == "CLOSED"
    assert opt["highest_milestone"] == 2
    assert datetime.fromisoformat(opt["t2_at"]) == t2_at.astimezone(timezone.utc)
    assert opt["exit"]["event_at"] == quote_at.isoformat()
    assert opt["exit"]["exited_fraction"] == pytest.approx(0.2)
    assert opt.get("deferred_option_exit") is None
    assert not opt.get("pending_index_observations")


@pytest.mark.asyncio
async def test_recovered_option_stop_before_deferred_trigger_wins_over_later_queued_t2():
    trade = _trade(phase="POST_T1")
    opt = trade["market_context"]["option_trailing"]
    opt.update(atr_enabled=True, accounting_formula_version="one_lot_reference_v1")
    pm = _manager(trade)
    pm._option_available_until = {}
    t2_at = datetime(2026, 11, 4, 10, 20, 30, tzinfo=IST)
    quote_at = datetime(2026, 11, 4, 10, 21, 0, tzinfo=IST)
    tick = {"price": trade["target_2"], "exchange_at": t2_at.isoformat(),
            "received_at": t2_at.isoformat(), "source": "dhan_tick"}
    quote = _quote(166.0, 21)
    quote.update(observed_at=quote_at.isoformat(), received_at=(quote_at + timedelta(seconds=1)).isoformat())

    start = datetime(2026, 11, 4, 9, 20, tzinfo=IST)
    stop_bar_end = datetime(2026, 11, 4, 10, 20, tzinfo=IST)
    history = []
    for i in range(13):
        bar_start = start + timedelta(minutes=5 * i)
        bar_end = bar_start + timedelta(minutes=5)
        close = 160.0 if bar_end == stop_bar_end else 170.0
        history.append(OptionCandle(bar_start, bar_end, 170.0, 171.0,
                                    min(169.0, close), close, 1, "39961"))

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        first_events = await pm.update_option_market_events(
            [], {}, current_time=quote_at + timedelta(seconds=2), index_ticks=[tick], option_quotes=[quote])
    assert ("v2-trade", "STOPPED_OUT_AT_BE") in first_events
    assert opt.get("deferred_option_exit") is not None

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        recovery_events = await pm.update_option_market_events(
            [], {"39961": history}, current_time=start + timedelta(minutes=65, seconds=2))

    assert ("v2-trade", "STOPPED_OUT_AT_BE") in recovery_events
    assert trade["state"] == "CLOSED"
    assert opt["highest_milestone"] == 1
    assert opt["remaining_fraction"] == 0.0
    assert opt["exit"]["event_at"] == stop_bar_end.isoformat()
    assert opt["exit"]["option_close"] == pytest.approx(160.0)
    assert opt["exit"]["exited_fraction"] == pytest.approx(0.5)
    assert opt.get("deferred_option_exit") is None
    assert not opt.get("pending_index_observations")


@pytest.mark.asyncio
@pytest.mark.parametrize("bar_close", [166.0, 167.20])
async def test_same_close_index_ambiguity_emits_v2_premium_protective_event(bar_close):
    trade = _trade(phase="POST_T1")
    opt = trade["market_context"]["option_trailing"]
    opt.update(atr_enabled=True, accounting_formula_version="one_lot_reference_v1",
               execution_status="CONFIRMED_PAPER_FILL")
    pm = _manager(trade)
    stop_bar_end = datetime(2026, 11, 4, 10, 20, tzinfo=IST)
    tick = {"price": trade["target_2"], "exchange_at": stop_bar_end.isoformat(),
            "received_at": stop_bar_end.isoformat(), "source": "dhan_tick"}
    start = datetime(2026, 11, 4, 9, 20, tzinfo=IST)
    history = []
    for i in range(13):
        bar_start = start + timedelta(minutes=5 * i)
        bar_end = bar_start + timedelta(minutes=5)
        close = bar_close if bar_end == stop_bar_end else 170.0
        history.append(OptionCandle(bar_start, bar_end, 170.0, 171.0,
                                    min(169.0, close), close, 1, "39961"))

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events(
            [], {"39961": history}, current_time=stop_bar_end + timedelta(seconds=2), index_ticks=[tick])

    assert ("v2-trade", "STOPPED_OUT_AT_BE") in events
    assert trade["state"] == "T1_HIT"
    assert opt.get("exit") is None
    ambiguity = opt["ambiguous_index_touch"]
    protective = ambiguity["protective_option_exit"]
    assert protective["stop_domain"] == "OPTION_PREMIUM"
    assert protective["reason"] == "STOPPED_OUT_AT_BE"
    assert protective["option_close"] == pytest.approx(bar_close)
    assert opt["execution_route_pending"] is True
    assert opt["execution_route_reason"] == "STOPPED_OUT_AT_BE"


@pytest.mark.asyncio
async def test_execution_route_acknowledgement_clears_only_matching_durable_marker():
    trade = _trade(phase="POST_T1")
    trade["state"] = "CLOSED"
    opt = trade["market_context"]["option_trailing"]
    opt.update(execution_route_pending=True, execution_route_reason="ATR_SL_HIT")
    pm = _manager(trade)
    pm.analytics.acknowledge_option_execution_route = Mock(
        return_value={"success": True, "acknowledged": True})
    pm._record_trade_update = AsyncMock()

    assert await pm.acknowledge_option_execution_route("v2-trade", "ATR_SL_HIT") is True
    assert opt["execution_route_pending"] is False
    assert opt["execution_route_reason"] == "ATR_SL_HIT"
    pm.analytics.acknowledge_option_execution_route.assert_called_once_with("v2-trade", "ATR_SL_HIT")
    pm._record_trade_update.assert_not_awaited()
    assert await pm.acknowledge_option_execution_route("v2-trade", "STOPPED_OUT_AT_BE") is False
    pm.analytics.acknowledge_option_execution_route.assert_called_once()


@pytest.mark.asyncio
async def test_execution_route_marker_stays_pending_until_terminal_safe_ack_commits():
    trade = _trade(phase="POST_T1")
    trade["state"] = "CLOSED"
    opt = trade["market_context"]["option_trailing"]
    opt.update(execution_route_pending=True, execution_route_reason="ATR_SL_HIT")
    pm = _manager(trade)
    pm.analytics.acknowledge_option_execution_route = Mock(
        return_value={"success": True, "acknowledged": False})

    assert await pm.acknowledge_option_execution_route("v2-trade", "ATR_SL_HIT") is False
    assert opt["execution_route_pending"] is True
    assert opt["execution_route_reason"] == "ATR_SL_HIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_offset", [-1, 0])
async def test_delayed_quote_at_or_before_t2_is_rejected_after_t2_committed(quote_offset):
    trade = _trade(phase="POST_T1")
    pm = _manager(trade)
    t2_at = datetime(2026, 11, 4, 10, 21, 1, tzinfo=IST)
    tick = {"price": trade["target_2"], "exchange_at": t2_at.isoformat(),
            "received_at": t2_at.isoformat(), "source": "dhan_tick"}
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        t2_events = await pm.update_option_market_events([], {}, current_time=t2_at, index_ticks=[tick])

    assert ("v2-trade", "T2_HIT") in t2_events
    assert trade["state"] == "T2_REACHED"
    quote_at = t2_at + timedelta(seconds=quote_offset)
    quote = _quote(167.2, 21)
    quote.update(observed_at=quote_at.isoformat(), received_at=(t2_at + timedelta(seconds=2)).isoformat())
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events(
            [], {}, current_time=t2_at + timedelta(seconds=2), option_quotes=[quote])

    opt = trade["market_context"]["option_trailing"]
    assert events == []
    assert trade["state"] == "T2_REACHED"
    assert opt["highest_milestone"] == 2
    assert opt["remaining_fraction"] == pytest.approx(0.2)
    assert opt.get("exit") is None
    assert opt.get("quote_watermark") is None
    assert opt.get("last_option_quote") is None
    assert opt["monitoring_status"] == "UNAVAILABLE"
    assert "T2 transition" in opt["monitoring_detail"]


@pytest.mark.asyncio
async def test_fresh_quote_is_not_discarded_after_history_fetch_finishes_late():
    trade = _trade(phase="POST_T1")
    pm = _manager(trade)
    observed_at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    received_at = observed_at + timedelta(seconds=2)
    quote = _quote(167.20, 21)
    quote.update(observed_at=observed_at.isoformat(), received_at=received_at.isoformat())

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events(
            [], {}, current_time=received_at + timedelta(seconds=10),
            quote_freshness_at=received_at + timedelta(seconds=1), option_quotes=[quote])

    assert ("v2-trade", "STOPPED_OUT_AT_BE") in events
    assert trade["market_context"]["option_trailing"]["exit"]["option_close"] == pytest.approx(167.20)


@pytest.mark.asyncio
async def test_post_t1_break_even_outcome_reports_zero_points_even_if_quote_gaps_below_cost():
    trade = _trade(phase="POST_T1")
    pm = _manager(trade)
    quote = _quote(160.0, 21)
    quote["observed_at"] = "2026-11-04T10:21:00+05:30"
    quote["received_at"] = quote["observed_at"]

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events(
            [], {}, current_time=datetime(2026, 11, 4, 10, 21, tzinfo=IST), option_quotes=[quote])

    assert ("v2-trade", "STOPPED_OUT_AT_BE") in events
    exit_info = trade["market_context"]["option_trailing"]["exit"]
    assert exit_info["runner_option_points"] == pytest.approx(0.0)
    assert exit_info["reference_exit_price"] == pytest.approx(160.0)


@pytest.mark.asyncio
async def test_post_t1_break_even_stop_is_not_replaced_by_atr_at_or_below_cost():
    trade = _trade(phase="POST_T1")
    pm = _manager(trade)
    at = datetime(2026, 11, 4, 10, 25, tzinfo=IST)
    bar = {"timestamp": (at - timedelta(minutes=5)).isoformat(), "end_time": at.isoformat(),
           "open": 167.2, "high": 168.0, "low": 165.0, "close": 166.5,
           "volume": 1, "security_id": "39961", "complete": True}
    await pm.update_option_atr_trailing({"39961": [bar]}, current_time=at)
    opt = trade["market_context"]["option_trailing"]
    assert opt["locked_option_sl"] >= 167.20


@pytest.mark.asyncio
@pytest.mark.parametrize(("spot", "expected_event", "expected_state"), [
    (23970.0, "T2_HIT", "T2_REACHED"),
    (23909.90, "STOPPED_OUT_AT_BE", "CLOSED"),
])
async def test_post_t1_index_milestones_progress_without_disabled_atr_history(
        spot, expected_event, expected_state):
    trade = _trade(phase="POST_T1")
    option = trade["market_context"]["option_trailing"]
    option["atr_enabled"] = False
    pm = _manager(trade)
    pm._option_available_until = {}
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    tick = {"price": spot, "exchange_at": at.isoformat(), "received_at": at.isoformat(),
            "source": "dhan_tick"}

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events([], {}, current_time=at, index_ticks=[tick])

    assert ("v2-trade", expected_event) in events
    assert trade["state"] == expected_state
    assert not option.get("pending_index_observations")


@pytest.mark.asyncio
async def test_delayed_quote_before_active_atr_stop_revision_cannot_trigger_retroactively():
    trade = _trade(phase="POST_T1")
    opt = trade["market_context"]["option_trailing"]
    opt.update(locked_option_sl=171.0, protection_mode="ATR", premium_stop_source="ATR_TRAILING",
               stop_effective_at="2026-11-04T10:20:00+05:30")
    pm = _manager(trade)
    delayed = _quote(169.0, 20)
    delayed["observed_at"] = "2026-11-04T10:19:59+05:30"
    delayed["received_at"] = "2026-11-04T10:20:02+05:30"

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events(
            [], {}, current_time=datetime(2026, 11, 4, 10, 20, 2, tzinfo=IST), option_quotes=[delayed])

    assert events == []
    assert trade["state"] == "T1_HIT"
    assert opt["quote_watermark"] is None
    assert opt["locked_option_sl"] == pytest.approx(171.0)
    assert opt["monitoring_status"] == "UNAVAILABLE"
    assert "out-of-order" in opt["monitoring_detail"]

    current = _quote(169.0, 20)
    current["observed_at"] = "2026-11-04T10:20:02+05:30"
    current["received_at"] = current["observed_at"]
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events(
            [], {}, current_time=datetime(2026, 11, 4, 10, 20, 2, tzinfo=IST), option_quotes=[current])
    assert ("v2-trade", "ATR_SL_HIT") in events


@pytest.mark.asyncio
async def test_t1_moves_premium_stop_to_entry_cost_without_changing_spot_guard():
    trade = _trade()
    pm = _manager(trade)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_trades(23930.0, event_time=datetime(2026, 11, 4, 9, 25, tzinfo=IST))
    assert ("v2-trade", "T1_HIT") in events
    opt = trade["market_context"]["option_trailing"]
    assert opt["locked_option_sl"] == pytest.approx(167.20)
    assert opt["premium_stop_source"] == "BREAK_EVEN_COST"
    assert trade["stop_loss"] == pytest.approx(trade["entry_price"])


@pytest.mark.asyncio
async def test_valid_post_t1_atr_candidate_can_only_raise_cost_floor():
    trade = _trade(phase="POST_T1")
    opt = trade["market_context"]["option_trailing"]
    opt["atr_enabled"] = True
    pm = _manager(trade)
    start = datetime(2026, 11, 4, 9, 15, tzinfo=IST)
    bars = [OptionCandle(start + timedelta(minutes=5*i), start + timedelta(minutes=5*(i+1)),
                         200, 201, 199, 200, 1, "39961") for i in range(16)]
    await pm.update_option_atr_trailing({"39961": bars}, current_time=bars[-1].end_time)
    assert opt["locked_option_sl"] > 167.20
    assert opt["premium_stop_source"] == "ATR_TRAILING"


@pytest.mark.asyncio
@pytest.mark.parametrize("option_type", ["CE", "PE"])
async def test_long_premium_stop_comparison_is_same_for_calls_and_puts(option_type):
    trade = _trade(option_type=option_type)
    pm = _manager(trade)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_option_market_events([], {}, current_time=datetime(2026, 11, 4, 9, 20, tzinfo=IST),
                                                       option_quotes=[_quote(160.0, 20, option_type=option_type)])
    assert ("v2-trade", "SL_HIT") in events


@pytest.mark.asyncio
async def test_wrong_contract_and_stale_quotes_do_not_change_protection_state():
    trade = _trade()
    pm = _manager(trade)
    wrong_contract = _quote(150.0, 20)
    wrong_contract["contract"]["strike"] = 23900
    stale = _quote(150.0, 20)
    stale["received_at"] = "2026-11-04T09:19:00+05:30"
    before = trade["market_context"]["option_trailing"].copy()
    await pm.update_option_market_events([], {}, current_time=datetime(2026, 11, 4, 9, 20, tzinfo=IST),
                                         option_quotes=[wrong_contract, stale])
    after = trade["market_context"]["option_trailing"]
    assert trade["state"] == "OPEN"
    assert after["quote_watermark"] == before["quote_watermark"] is None
    assert after["locked_option_sl"] == before["locked_option_sl"] == 161.05


@pytest.mark.asyncio
async def test_expired_contract_quote_cannot_trigger_premium_stop():
    trade = _trade(phase="POST_T1")
    pm = _manager(trade)
    quote = _quote(160.0, 21)
    quote["observed_at"] = "2026-11-10T09:21:00+05:30"
    quote["received_at"] = quote["observed_at"]

    events = await pm.update_option_market_events(
        [], {}, current_time=datetime(2026, 11, 10, 9, 21, tzinfo=IST), option_quotes=[quote])

    assert events == []
    assert trade["state"] == "T1_HIT"
    assert trade["market_context"]["option_trailing"].get("quote_watermark") is None


def test_break_even_alert_names_premium_domain_and_zero_runner_points():
    trade = _trade(phase="POST_T1")
    opt = trade["market_context"]["option_trailing"]
    opt.update(phase="CLOSED", monitoring_status="CLOSED", remaining_fraction=0.0,
               exit={"reason": "STOPPED_OUT_AT_BE", "stop_domain": "OPTION_PREMIUM",
                     "event_at": "2026-11-04T10:21:00+05:30", "option_close": 167.2,
                     "reference_exit_price": 167.2, "option_stop": 167.2,
                     "runner_option_points": 0.0, "fill_price": None},
               exit_trigger={"domain": "OPTION_PREMIUM", "source": "dhan_last_trade",
                             "event_at": "2026-11-04T10:21:00+05:30", "price": 167.2,
                             "effective_stop": 167.2})
    rendered = render_option_lifecycle_event({"update_type": "STOPPED_OUT_AT_BE", "event_time": opt["exit"]["event_at"],
                                              "trade": trade, "option_trailing": opt})
    text = rendered["embeds"][0]["description"]
    assert "Option-premium break-even stop hit" in rendered["embeds"][0]["title"]
    assert "Premium stop domain: OPTION_PREMIUM" in text
    assert "Runner stop-out points: +0.00" in text
    assert "Confirmed fill: unavailable" in text


@pytest.mark.asyncio
async def test_real_premium_closure_can_enrich_later_contemporaneous_spot_quote():
    trade = _trade(phase='POST_T1')
    opt = trade['market_context']['option_trailing']
    opt['accounting_formula_version'] = 'one_lot_reference_v1'
    pm = _manager(trade)
    at = datetime(2026, 11, 4, 10, 22, 23, tzinfo=IST)
    with patch('position_manager.send_option_lifecycle_event', new=AsyncMock(return_value='DELIVERED')):
        await pm._close_option_premium_trade(trade, 166, at, source='dhan_last_trade')
    leg = opt['accounting']['legs']['EXIT']
    assert leg['spot_price'] is None
    pm.set_reference_price_evidence(spot_quotes=[{'observed_at': at.isoformat(), 'price': 23920,
                                                  'source': 'dhan_tick'}])
    pm._enrich_reference_trade(trade)
    assert leg['spot_price'] == 23920
    assert leg['spot_approximate'] is False
    assert leg['option_source'] == 'dhan_last_trade'
    assert leg['trigger_price'] == 166
    assert opt['exit']['runner_option_points'] == 0.0


@pytest.mark.asyncio
async def test_atr_disabled_quote_health_does_not_flap_for_missing_history():
    trade = _trade(phase='POST_T1')
    opt = trade['market_context']['option_trailing']
    opt['atr_enabled'] = False
    pm = _manager(trade)
    pm._option_confirmed_sessions = {datetime(2026, 11, 4).date()}
    with patch('position_manager.send_option_lifecycle_event', new=AsyncMock(return_value='DELIVERED')):
        for second in (0, 1):
            at = datetime(2026, 11, 4, 10, 22, second, tzinfo=IST)
            quote = _quote(180, 22)
            quote.update(observed_at=at.isoformat(), received_at=at.isoformat())
            await pm.update_option_market_events([], {}, current_time=at, option_quotes=[quote])
            assert opt['monitoring_status'] == 'ACTIVE'
        assert not any(row.get('update_type') in {'MONITORING_UNAVAILABLE', 'MONITORING_RESTORED'}
                       for row in pm.analytics.rows)
        await pm.mark_option_quote_unavailable({'39961': opt['contract']}, 'provider failed', at)
        await pm.update_option_market_events([], {}, current_time=at)
        assert opt['monitoring_status'] == 'UNAVAILABLE'


@pytest.mark.asyncio
async def test_healthy_atr_history_does_not_restore_an_unavailable_quote_monitor():
    trade = _trade(phase="POST_T1")
    opt = trade["market_context"]["option_trailing"]
    opt["atr_enabled"] = True
    pm = _manager(trade)
    now = datetime(2026, 11, 4, 10, 30, 1, tzinfo=IST)
    pm._option_confirmed_sessions = {now.date()}
    bar = OptionCandle(now.replace(minute=25, second=0, microsecond=0),
                       now.replace(second=0, microsecond=0), 180, 181, 179, 180, 1, "39961")

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.mark_option_quote_unavailable({"39961": opt["contract"]}, "provider failed", now)
        await pm.update_option_atr_trailing({"39961": [bar]}, current_time=now)

    assert opt["monitoring_status"] == "UNAVAILABLE"
    assert opt["monitoring_unavailable_sources"] == ["OPTION_QUOTE"]
    assert opt["monitoring_detail"] == "provider failed"
    assert opt.get("last_option_quote") is None
    assert [row.get("update_type") for row in pm.analytics.rows].count("MONITORING_UNAVAILABLE") == 1
    assert not any(row.get("update_type") == "MONITORING_RESTORED" for row in pm.analytics.rows)

    quote = _quote(180, 30)
    quote.update(observed_at=now.isoformat(), received_at=now.isoformat())
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_option_market_events([], {}, current_time=now, option_quotes=[quote])

    assert opt["monitoring_status"] == "ACTIVE"
    assert opt["monitoring_unavailable_sources"] == []
    assert opt["last_option_quote"]["price"] == 180
    assert [row.get("update_type") for row in pm.analytics.rows].count("MONITORING_RESTORED") == 1

    later = now + timedelta(minutes=2)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_option_atr_trailing({}, current_time=later)
    assert opt["monitoring_status"] == "UNAVAILABLE"
    assert opt["monitoring_unavailable_sources"] == ["OPTION_HISTORY"]

    quote = _quote(180, 32)
    quote.update(observed_at=later.isoformat(), received_at=later.isoformat())
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_option_market_events([], {}, current_time=later, option_quotes=[quote])
    assert opt["monitoring_status"] == "UNAVAILABLE"
    assert opt["monitoring_unavailable_sources"] == ["OPTION_HISTORY"]
    assert [row.get("update_type") for row in pm.analytics.rows].count("MONITORING_RESTORED") == 1

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_option_atr_trailing({"39961": [bar]}, current_time=later + timedelta(seconds=1))
    assert opt["monitoring_status"] == "ACTIVE"
    assert opt["monitoring_unavailable_sources"] == []
    assert [row.get("update_type") for row in pm.analytics.rows].count("MONITORING_RESTORED") == 2
