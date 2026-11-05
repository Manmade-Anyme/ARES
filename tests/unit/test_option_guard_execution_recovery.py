"""Index execution protection must survive missing option history and restart."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from execution.order_manager import OrderManager
from execution.signal_dispatch import SignalDispatcher
from models import Direction, OptionTrailingContext
from tests.unit.test_manm176_dispatch import candidate, chain
from tests.unit.test_option_premium_protection_v2 import _trade, _manager, IST
from tests.unit.test_option_lifecycle_commit_boundary import Ledger


class GuardLedger(Ledger):
    def acknowledge_option_execution_route(self, trade_id, reason):
        option = self.rows[-1]["option_trailing"]
        assert option["execution_route_reason"] == reason
        option["execution_route_pending"] = False
        return {"success": True, "acknowledged": True}


def paper_guard_runtime(option_type="CE", *, pending_t1=False):
    trade = _trade(phase="POST_T1", entry=100, stop=95)
    trade.update(entry_price=24000, stop_loss=24000, target_1=24020, target_2=24040)
    option = trade["market_context"]["option_trailing"]
    signal = candidate()
    if option_type == "PE":
        trade.update(direction="BEARISH", target_1=23980, target_2=23960)
        signal.direction = Direction.BEARISH
        signal.option_type = "PE"
        signal.bracket = replace(signal.bracket, option_type="PE", synthetic_spot_sl=24010,
                                 spot_target_1=23980, spot_target_2=23960)
    trade["signal_uuid"] = str(signal.id)
    option.update(atr_enabled=True, execution_status="CONFIRMED_PAPER_FILL",
                  accounting_formula_version="one_lot_reference_v1", index_guard_stop=24000,
                  contract={"security_id": "123", "exchange_segment": "NSE_FNO",
                            "expiry": "2099-01-01", "strike": 24000, "option_type": option_type})
    positions = _manager(trade)
    positions._option_available_until = {}
    positions.analytics = GuardLedger()
    async def persist_execution(signal, execution):
        positions.persisted_execution = deepcopy(execution)
        return True
    positions.sync_execution = AsyncMock(side_effect=persist_execution)
    orders = OrderManager()
    record = orders.dispatch_bracket(signal.bracket)
    orders.observe_quote(record.correlation_id, 100, spot=24000, bid=100)
    orders.on_spot(record.correlation_id, trade["target_1"])
    if pending_t1:
        assert record.status == "EXIT_PENDING" and record.exit_reason == "T1_HIT"
        assert not record.t1_hit and record.pending_exit_quantity == 65
    else:
        orders.observe_quote(record.correlation_id, 100, spot=trade["target_1"], bid=100)
        assert record.t1_hit and record.exited_quantity == 65 and record.status == "FILLED"
    signal.trade_id = trade["id"]
    signal.market_context = trade["market_context"]
    dispatcher = SignalDispatcher(orders, positions)
    dispatcher._signals[record.correlation_id] = (signal, None)
    dispatcher._publish(record)
    positions.persisted_execution = deepcopy(trade["market_context"]["execution"])
    return trade, positions, dispatcher, record


async def guard_monitor(positions, dispatcher, at):
    import main
    fetcher = SimpleNamespace(fetch_option_candles=AsyncMock(return_value=[]),
                              fetch_option_observations=AsyncMock(return_value=[]))
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await main._monitor_completed_atr(
            [], positions, dispatcher, price_fetcher=fetcher, current_time=at,
            index_ticks=[{"price": 24000, "exchange_at": at.isoformat(), "received_at": at.isoformat()}])


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_before_route", [False, True])
@pytest.mark.parametrize("ambiguity", [None, "ambiguous_premium_bar", "ambiguous_premium_quote"])
@pytest.mark.parametrize("option_type", ["CE", "PE"])
async def test_missing_history_routes_index_guard_and_restores_pending_execution(
        crash_before_route, ambiguity, option_type):
    import main
    trade, positions, dispatcher, record = paper_guard_runtime(option_type)
    option = trade["market_context"]["option_trailing"]
    if ambiguity:
        option[ambiguity] = {"status": "T1_ORDER_UNKNOWN", "ordering_reason": "COST_STOP_ONLY_BREACH",
                             "event_at": option["t1_at"], "price": 99}
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    await guard_monitor(positions, None if crash_before_route else dispatcher, at)

    assert trade["state"] == "T1_HIT"
    assert option["exit"] is None
    intent = deepcopy(option.get("execution_route_trigger"))
    assert intent is not None
    assert intent["reason"] == "STOPPED_OUT_AT_BE"
    assert intent["stop_domain"] == "SPOT_INDEX"
    assert datetime.fromisoformat(intent["event_at"]) == at
    assert option["execution_route_pending"] is crash_before_route
    if not crash_before_route:
        assert record.status == "EXIT_PENDING"
        assert record.pending_exit_quantity == 65
        assert record.exited_quantity == 65
    await guard_monitor(positions, None, at + timedelta(seconds=1))
    assert option["execution_route_trigger"] == intent

    # Recover from independently serialized durable lifecycle/execution state.
    restored_trade = deepcopy(trade)
    restored_trade["market_context"]["option_trailing"] = OptionTrailingContext.from_dict(
        positions.analytics.rows[-1]["option_trailing"]).to_dict()
    restored_trade["market_context"]["execution"] = deepcopy(positions.persisted_execution)
    restored = _manager(restored_trade)
    restored.analytics = positions.analytics
    restored.sync_execution = AsyncMock(return_value=True)
    resumed = SignalDispatcher(OrderManager(), restored)
    await main._monitor_completed_atr([], restored, resumed, current_time=at + timedelta(seconds=2))
    restored_record = resumed.orders.records[record.correlation_id]
    assert restored_record.status == "EXIT_PENDING"
    assert restored_record.pending_exit_quantity == 65
    assert restored_trade["state"] == "T1_HIT"
    side = option_type.lower()
    await resumed.on_snapshot([{"strike": 24000, "expiry_date": "2099-01-01",
                                f"{side}_security_id": "123", f"{side}_ask": 100,
                                f"{side}_bid": 99.95, "underlying_spot": 24000}], 24000)
    assert restored_record.status == "CLOSED"
    assert restored_record.exited_quantity == 130
    assert restored_record.exit_reason == "STOPPED_OUT_AT_BE"
    await main._monitor_completed_atr([], restored, resumed, current_time=at + timedelta(seconds=3))
    assert resumed._signals == {}
    assert restored_trade["state"] == "T1_HIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [
    "reference_only", "reference_triggered", "no_t1", "unprotected", "live", "closed",
    "zero_fill", "guard_disabled", "wrong_guard", "wrong_contract", "missing_correlation",
    "wrong_signal", "stale_watermark", "before_t1", "bad_watermark",
])
async def test_missing_history_guard_rejects_unconfirmed_or_stale_execution(damage):
    trade, positions, dispatcher, record = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    if damage == "reference_only":
        option["execution_status"] = "REFERENCE_ONLY"
    elif damage == "reference_triggered":
        option["execution_status"] = "REFERENCE_TRIGGERED_NO_BROKER_FILL"
    elif damage == "no_t1":
        record.t1_hit = False
    elif damage == "unprotected":
        record.protection_confirmed = False
    elif damage == "live":
        record.mode = "LIVE"
    elif damage == "closed":
        record.status = "CLOSED"
        record.exited_quantity = record.filled_quantity
    elif damage == "zero_fill":
        record.filled_quantity = 0
    elif damage == "guard_disabled":
        option["index_guard_enabled"] = False
    elif damage == "wrong_guard":
        option["index_guard_stop"] = 23999
    elif damage == "wrong_contract":
        option["contract"]["security_id"] = "456"
    elif damage == "wrong_signal":
        trade["signal_uuid"] = "another-signal"
    elif damage == "stale_watermark":
        option["last_index_event_at"] = at.isoformat()
    elif damage == "before_t1":
        at = datetime.fromisoformat(option["t1_at"]) - timedelta(seconds=2)
    elif damage == "bad_watermark":
        option["last_index_event_at"] = "bad timestamp"
    dispatcher._publish(record)
    if damage == "missing_correlation":
        trade["market_context"]["execution"]["correlation_id"] = None

    await guard_monitor(positions, None, at)

    assert option.get("execution_route_trigger") is None
    assert option.get("execution_route_pending") is not True
    assert trade["state"] == "T1_HIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [False, True])
async def test_existing_premium_execution_intent_is_preserved_during_history_gap(acknowledged):
    trade, positions, dispatcher, record = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    premium = {"reason": "ATR_SL_HIT", "stop_domain": "OPTION_PREMIUM",
               "event_at": "2026-11-04T10:20:59+05:30", "option_close": 99}
    option.update(deferred_option_exit=deepcopy(premium),
                  execution_route_pending=not acknowledged, execution_route_reason="ATR_SL_HIT",
                  execution_status="EXIT_TRIGGERED_PENDING_CONFIRMATION",
                  pending_index_observations=[{"event_at": "2026-11-04T10:20:30+05:30",
                                               "price": 24040, "touches_t2": True}])
    dispatcher.orders.request_option_exit(record.correlation_id, "ATR_SL_HIT")
    dispatcher._publish(record)

    await guard_monitor(positions, dispatcher, datetime(2026, 11, 4, 10, 21, tzinfo=IST))

    assert option.get("execution_route_trigger") is None
    assert option["deferred_option_exit"] == premium
    assert option["execution_route_reason"] == "ATR_SL_HIT"
    assert record.exit_reason == "ATR_SL_HIT"
    assert record.pending_exit_quantity == 65


@pytest.mark.asyncio
@pytest.mark.parametrize("option_type", ["CE", "PE"])
async def test_guard_expands_pending_t1_partial_before_its_bid(option_type):
    trade, positions, dispatcher, record = paper_guard_runtime(option_type, pending_t1=True)

    await guard_monitor(positions, dispatcher, datetime(2026, 11, 4, 10, 21, tzinfo=IST))

    assert trade["state"] == "T1_HIT"
    assert record.status == "EXIT_PENDING"
    assert record.exit_reason == "STOPPED_OUT_AT_BE"
    assert record.pending_exit_quantity == 130
    assert record.exited_quantity == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_t1", [False, True])
async def test_guard_accepts_valid_legacy_paper_execution_snapshot(pending_t1):
    trade, positions, dispatcher, record = paper_guard_runtime(pending_t1=pending_t1)
    trade["market_context"]["execution"].pop("order_record")

    await guard_monitor(positions, dispatcher, datetime(2026, 11, 4, 10, 21, tzinfo=IST))

    assert trade["state"] == "T1_HIT"
    assert record.exit_reason == "STOPPED_OUT_AT_BE"
    assert record.pending_exit_quantity == (130 if pending_t1 else 65)


@pytest.mark.asyncio
async def test_guard_does_not_route_pre_t1_wick_from_t1_candle():
    trade, positions, _, _ = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    option["t1_at"] = "2026-11-04T10:05:30+05:30"
    candle_start = datetime(2026, 11, 4, 10, 5, tzinfo=IST)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await positions.update_trades(24010, candle_high=24015, candle_low=24000,
                                                candle_timestamp=candle_start, only_option_policy=True)
    assert (trade["id"], "STOPPED_OUT_AT_BE") not in events
    assert option.get("execution_route_pending") is not True
    assert option.get("execution_route_trigger") is None


@pytest.mark.asyncio
async def test_guard_routes_t1_candle_close_that_touches_entry():
    trade, positions, _, _ = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    option["t1_at"] = "2026-11-04T10:05:30+05:30"
    candle_start = datetime(2026, 11, 4, 10, 5, tzinfo=IST)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await positions.update_trades(24000, candle_high=24015, candle_low=24000,
                                                candle_timestamp=candle_start, only_option_policy=True)
    assert (trade["id"], "STOPPED_OUT_AT_BE") in events
    assert option["execution_route_pending"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("option_type", ["CE", "PE"])
@pytest.mark.parametrize("close_hits_guard", [False, True])
@pytest.mark.parametrize("unresolved_premium", [False, True])
async def test_t1_candle_replay_uses_close_for_guard_execution(option_type, close_hits_guard, unresolved_premium):
    trade, positions, _, _ = paper_guard_runtime(option_type)
    option = trade["market_context"]["option_trailing"]
    option["t1_at"] = "2026-11-04T10:20:30+05:30"
    if unresolved_premium:
        option["atr_enabled"] = False
        option["ambiguous_premium_quote"] = {"status": "T1_ORDER_UNKNOWN",
            "ordering_reason": "COST_STOP_ONLY_BREACH", "event_at": option["t1_at"], "price": 99}
    start = datetime(2026, 11, 4, 10, 20, tzinfo=IST)
    if option_type == "CE":
        candle = {"open": 24010, "high": 24015, "low": 23999,
                  "close": 24000 if close_hits_guard else 24010}
    else:
        candle = {"open": 23990, "high": 24001, "low": 23985,
                  "close": 24000 if close_hits_guard else 23990}
    candle.update(timestamp=start.isoformat(), complete=True)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await positions.update_option_market_events(
            [candle], {}, current_time=start + timedelta(minutes=1))
    assert ((trade["id"], "STOPPED_OUT_AT_BE") in events) is close_hits_guard
    assert bool(option.get("execution_route_pending")) is close_hits_guard
    assert trade["state"] == "T1_HIT"


@pytest.mark.asyncio
async def test_guard_intent_persists_after_same_timestamp_watermark():
    from storage import to_utc_iso

    trade, positions, _, _ = paper_guard_runtime()
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    stamp = to_utc_iso(at)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await positions._record_trade_update(trade, None, None, stamp)
        await positions.update_trades(24000, event_time=at, market_event_time=at,
                                      only_option_policy=True)

    assert positions.analytics.rows[-1]["option_trailing"]["execution_route_pending"] is True
    assert positions.analytics.rows[-1]["option_trailing"]["execution_route_trigger"]["stop_domain"] == "SPOT_INDEX"


@pytest.mark.asyncio
async def test_malformed_route_trigger_does_not_block_other_trades():
    import main

    trade, _, dispatcher, _ = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    option.update(execution_route_trigger="invalid", execution_route_pending=True,
                  execution_route_reason="STOPPED_OUT_AT_BE")
    assert await dispatcher.on_option_stop(trade, "STOPPED_OUT_AT_BE") is False

    good = {"id": "good", "market_context": {"option_trailing": {
        "policy_version": "option_premium_protection_v2", "highest_milestone": 1,
        "execution_route_pending": True, "execution_route_reason": "ATR_SL_HIT",
        "exit": {"reason": "ATR_SL_HIT", "stop_domain": "OPTION_PREMIUM"},
    }}}
    positions = SimpleNamespace(active_trades=[], _option_execution_route_trades=[trade, good],
                                update_atr_trailing=AsyncMock())
    routes = SimpleNamespace(on_candles=AsyncMock(), on_option_stop=AsyncMock(return_value=True))
    await main._monitor_completed_atr([], positions, routes,
                                      current_time=datetime(2026, 11, 4, 10, 21, tzinfo=IST))
    routes.on_option_stop.assert_awaited_once_with(good, "ATR_SL_HIT")


def test_malformed_ambiguity_cannot_create_guard_intent():
    trade, _, _, _ = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    option["ambiguous_premium_bar"] = "invalid"
    at = "2026-11-04T10:21:00+05:30"
    from position_manager import PositionManager
    assert PositionManager._retain_index_guard_execution_route(trade, at, at) is False
    assert option.get("execution_route_pending") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_existing_queued_guard_gets_new_execution_intent(ambiguous):
    trade, positions, _, _ = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    option["execution_status"] = "REFERENCE_ONLY"
    if ambiguous:
        option["ambiguous_premium_bar"] = {"status": "T1_ORDER_UNKNOWN",
            "ordering_reason": "COST_STOP_ONLY_BREACH", "event_at": option["t1_at"], "price": 99}
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await positions.update_trades(24000, event_time=at, market_event_time=at, only_option_policy=True)
        await positions.update_trades(24000, event_time=at, market_event_time=at, only_option_policy=True)
        option["execution_status"] = "CONFIRMED_PAPER_FILL"
        await positions.update_trades(24000, event_time=at, market_event_time=at, only_option_policy=True)
    assert positions.analytics.rows[-1]["option_trailing"]["execution_route_pending"] is True
    assert option["execution_route_trigger"]["stop_domain"] == "SPOT_INDEX"


@pytest.mark.asyncio
@pytest.mark.parametrize("option_type", ["CE", "PE"])
@pytest.mark.parametrize("ambiguity_field", ["ambiguous_premium_bar", "ambiguous_premium_quote", "ambiguous_index_touch"])
@pytest.mark.parametrize("candle_input", [False, True])
async def test_guard_routes_while_reference_ambiguity_remains(option_type, ambiguity_field, candle_input):
    import main
    trade, positions, dispatcher, record = paper_guard_runtime(option_type)
    option = trade["market_context"]["option_trailing"]
    option["atr_enabled"] = False
    if ambiguity_field == "ambiguous_index_touch":
        option[ambiguity_field] = {"kind": "T2_ENTRY", "status": "ORDER_UNKNOWN",
                                   "event_at": "2026-11-04T10:20:00+05:30"}
    else:
        option[ambiguity_field] = {"status": "T1_ORDER_UNKNOWN", "ordering_reason": "COST_STOP_ONLY_BREACH",
                                   "event_at": option["t1_at"], "price": 99}
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    candles = []
    ticks = []
    if candle_input:
        candles = [{"timestamp": at.isoformat(), "open": 24000, "high": 24001,
                    "low": 23999, "close": 24000, "complete": True}]
        now = at + timedelta(minutes=1)
    else:
        ticks = [{"price": 24000, "exchange_at": at.isoformat(), "received_at": at.isoformat()}]
        now = at
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await positions.update_option_market_events(candles, {}, current_time=now, index_ticks=ticks)
        await main._monitor_completed_atr([], positions, dispatcher, current_time=now)
    assert (trade["id"], "STOPPED_OUT_AT_BE") in events
    assert trade["state"] == "T1_HIT"
    assert option[ambiguity_field]["subsequent_index_observations"]
    assert record.status == "EXIT_PENDING"
    assert record.pending_exit_quantity == 65
    assert record.exit_reason == "STOPPED_OUT_AT_BE"


@pytest.mark.asyncio
async def test_new_t1_entry_ambiguity_routes_proven_close_guard_exit():
    import main
    trade, positions, dispatcher, record = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    option["atr_enabled"] = False
    option["t1_at"] = datetime(2026, 11, 4, 10, 20, 30, tzinfo=IST).isoformat()
    start = datetime(2026, 11, 4, 10, 20, tzinfo=IST)
    completed = start + timedelta(minutes=1)
    candle = {"timestamp": start.isoformat(), "open": 24010, "high": 24025,
              "low": 23995, "close": 23999, "complete": True}

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await positions.update_option_market_events([candle], {}, current_time=completed)

    assert option["ambiguous_index_touch"]["kind"] == "T1_ENTRY"
    assert trade["state"] == "T1_HIT"
    assert (trade["id"], "STOPPED_OUT_AT_BE") in events
    assert option["execution_route_pending"] is True
    assert option["execution_route_trigger"]["stop_domain"] == "SPOT_INDEX"
    assert datetime.fromisoformat(option["execution_route_trigger"]["event_at"]) == completed
    committed = positions.analytics.rows[-1]["option_trailing"]
    assert committed["ambiguous_index_touch"]["kind"] == "T1_ENTRY"
    assert committed["execution_route_pending"] is True

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await main._monitor_completed_atr([], positions, dispatcher, current_time=completed)
    assert record.status == "EXIT_PENDING"
    assert record.pending_exit_quantity == 65
    assert record.exit_reason == "STOPPED_OUT_AT_BE"
    assert trade["state"] == "T1_HIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("option_type", ["CE", "PE"])
async def test_first_t1_candle_routes_guard_for_already_t1_booked_paper_execution(option_type):
    import main
    trade, positions, dispatcher, record = paper_guard_runtime(option_type)
    option = trade["market_context"]["option_trailing"]
    option.update(
        phase="PRE_T1", highest_milestone=0, booked_fraction=0.0,
        remaining_fraction=1.0, locked_option_sl=option["original_option_sl"],
        protection_mode="INITIAL_OPTION_SL", index_guard_enabled=False,
        index_guard_stop=None, t1_at=None, atr_enabled=False,
    )
    trade["state"] = "OPEN"
    assert trade["market_context"]["execution"]["t1_hit"] is True

    start = datetime(2026, 11, 4, 10, 20, tzinfo=IST)
    completed = start + timedelta(minutes=1)
    candle = ({"timestamp": start.isoformat(), "open": 24010, "high": 24025,
               "low": 23995, "close": 23999, "complete": True}
              if option_type == "CE" else
              {"timestamp": start.isoformat(), "open": 23990, "high": 24025,
               "low": 23975, "close": 24001, "complete": True})

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await positions.update_option_market_events([candle], {}, current_time=completed)

    assert option["ambiguous_index_touch"]["kind"] == "T1_ENTRY"
    assert trade["state"] == "T1_HIT"
    assert (trade["id"], "STOPPED_OUT_AT_BE") in events
    assert option["execution_route_pending"] is True
    assert option["execution_route_trigger"]["stop_domain"] == "SPOT_INDEX"
    assert datetime.fromisoformat(option["execution_route_trigger"]["event_at"]) == completed
    committed = positions.analytics.rows[-1]["option_trailing"]
    assert committed["ambiguous_index_touch"]["kind"] == "T1_ENTRY"
    assert committed["execution_route_pending"] is True

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await main._monitor_completed_atr([], positions, dispatcher, current_time=completed)
    assert record.status == "EXIT_PENDING"
    assert record.pending_exit_quantity == 65
    assert record.exit_reason == "STOPPED_OUT_AT_BE"
    assert trade["state"] == "T1_HIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("option_type", ["CE", "PE"])
async def test_new_t2_entry_ambiguity_routes_paper_guard_exit(option_type):
    import main
    trade, positions, dispatcher, record = paper_guard_runtime(option_type)
    option = trade["market_context"]["option_trailing"]
    option["atr_enabled"] = False
    start = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    completed = start + timedelta(minutes=1)
    if option_type == "CE":
        open_price, high, low, close = 24010, 24045, 23995, 23999
        later_price = 24001
    else:
        open_price, high, low, close = 24000, 24005, 23955, 24001
        later_price = 23999
    candle = {"timestamp": start.isoformat(), "open": open_price, "high": high,
              "low": low, "close": close, "complete": True}

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await positions.update_option_market_events(
            [candle], {}, current_time=completed)

    ambiguity = option["ambiguous_index_touch"]
    assert ambiguity["kind"] == "T2_ENTRY"
    assert ambiguity["status"] == "ORDER_UNKNOWN"
    assert trade["state"] == "T1_HIT"
    assert option["highest_milestone"] == 1
    assert option["booked_fraction"] == pytest.approx(0.5)
    assert option["remaining_fraction"] == pytest.approx(0.5)
    assert option.get("t2_at") is None
    assert (trade["id"], "STOPPED_OUT_AT_BE") in events
    assert option["execution_route_pending"] is True
    trigger = deepcopy(option["execution_route_trigger"])
    assert trigger["stop_domain"] == "SPOT_INDEX"
    assert datetime.fromisoformat(trigger["event_at"]) == completed

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await main._monitor_completed_atr([], positions, dispatcher, current_time=completed)
    assert record.status == "EXIT_PENDING"
    assert record.pending_exit_quantity == 65
    assert record.exit_reason == "STOPPED_OUT_AT_BE"

    # A later observation that clears the guard but stays short of T2 cannot
    # replace the first protective trigger or finalize reference ordering.
    later = completed + timedelta(seconds=1)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        later_events = await positions.update_option_market_events(
            [], {}, current_time=later,
            index_ticks=[{"price": later_price, "exchange_at": later.isoformat(),
                          "received_at": later.isoformat()}])
    assert later_events == []
    assert option["ambiguous_index_touch"]["kind"] == "T2_ENTRY"
    assert option["execution_route_trigger"] == trigger
    assert trade["state"] == "T1_HIT"
    assert record.status == "EXIT_PENDING"


@pytest.mark.asyncio
async def test_new_t2_entry_ambiguity_does_not_route_reference_only_trade():
    trade, positions, _, _ = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    option.update(atr_enabled=False, execution_status="REFERENCE_ONLY")
    start = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    candle = {"timestamp": start.isoformat(), "open": 24010, "high": 24045,
              "low": 23995, "close": 23999, "complete": True}
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await positions.update_option_market_events(
            [candle], {}, current_time=start + timedelta(minutes=1))

    assert events == []
    assert trade["state"] == "T1_HIT"
    assert option["ambiguous_index_touch"]["kind"] == "T2_ENTRY"
    assert option.get("execution_route_pending") is not True
    assert option.get("execution_route_trigger") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["reference_only", "no_execution_t1", "stale"])
async def test_ambiguous_reference_guard_rejects_invalid_execution_intent(damage):
    trade, positions, dispatcher, record = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    option.update(atr_enabled=False, ambiguous_premium_quote={"status": "T1_ORDER_UNKNOWN",
                  "ordering_reason": "COST_STOP_ONLY_BREACH", "event_at": option["t1_at"], "price": 99})
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    if damage == "reference_only":
        option["execution_status"] = "REFERENCE_ONLY"
    elif damage == "no_execution_t1":
        record.t1_hit = False
        dispatcher._publish(record)
    else:
        option["last_index_event_at"] = at.isoformat()
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await positions.update_trades(24000, event_time=at, market_event_time=at, only_option_policy=True)
    assert (trade["id"], "STOPPED_OUT_AT_BE") not in events
    assert option.get("execution_route_pending") is not True
    assert record.status == "FILLED"


@pytest.mark.asyncio
async def test_ambiguous_candle_guard_checks_prior_watermark_against_source_time():
    trade, positions, _, record = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    option.update(atr_enabled=False, last_index_event_at="2026-11-04T10:21:00+05:30",
                  ambiguous_premium_quote={"status": "T1_ORDER_UNKNOWN", "price": 99,
                      "ordering_reason": "COST_STOP_ONLY_BREACH", "event_at": option["t1_at"]})
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await positions.update_trades(
            24000, candle_high=24001, candle_low=23999, candle_timestamp=at - timedelta(minutes=1),
            event_time=at + timedelta(seconds=1), market_event_time=at - timedelta(seconds=30),
            only_option_policy=True)
    assert (trade["id"], "STOPPED_OUT_AT_BE") not in events
    assert option.get("execution_route_pending") is not True
    assert record.status == "FILLED"


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguity_field", ["ambiguous_premium_quote", "ambiguous_index_touch"])
async def test_ambiguous_guard_persists_intent_for_already_retained_observation(ambiguity_field):
    trade, positions, _, _ = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    option.update(atr_enabled=False, execution_status="REFERENCE_ONLY")
    if ambiguity_field == "ambiguous_index_touch":
        option[ambiguity_field] = {"kind": "T2_ENTRY", "status": "ORDER_UNKNOWN",
                                   "event_at": "2026-11-04T10:20:00+05:30"}
    else:
        option[ambiguity_field] = {"status": "T1_ORDER_UNKNOWN", "ordering_reason": "COST_STOP_ONLY_BREACH",
                                   "event_at": option["t1_at"], "price": 99}
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await positions.update_trades(24000, event_time=at, market_event_time=at, only_option_policy=True)
        await positions.update_trades(24000, event_time=at, market_event_time=at, only_option_policy=True)
        option["execution_status"] = "CONFIRMED_PAPER_FILL"
        events = await positions.update_trades(24000, event_time=at, market_event_time=at, only_option_policy=True)
    assert (trade["id"], "STOPPED_OUT_AT_BE") in events
    assert positions.analytics.rows[-1]["option_trailing"]["execution_route_pending"] is True
    assert len(option[ambiguity_field]["subsequent_index_observations"]) == 1


@pytest.mark.asyncio
async def test_ambiguous_reference_guard_preserves_existing_premium_route():
    trade, positions, dispatcher, record = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    premium = {"reason": "ATR_SL_HIT", "stop_domain": "OPTION_PREMIUM",
               "event_at": "2026-11-04T10:20:59+05:30", "option_close": 99}
    option.update(atr_enabled=False, execution_status="EXIT_TRIGGERED_PENDING_CONFIRMATION",
                  execution_route_pending=True, execution_route_reason="ATR_SL_HIT",
                  ambiguous_premium_bar={"status": "T1_ORDER_UNKNOWN", "protective_option_exit": premium})
    dispatcher.orders.request_option_exit(record.correlation_id, "ATR_SL_HIT")
    dispatcher._publish(record)
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await positions.update_trades(24000, event_time=at, market_event_time=at, only_option_policy=True)
    assert option["execution_route_reason"] == "ATR_SL_HIT"
    assert option.get("execution_route_trigger") is None
    assert record.exit_reason == "ATR_SL_HIT"


@pytest.mark.asyncio
async def test_index_guard_route_retries_failed_sync_without_replacing_first_intent():
    import main
    trade, positions, dispatcher, record = paper_guard_runtime()
    option = trade["market_context"]["option_trailing"]
    positions.sync_execution = AsyncMock(side_effect=[False, True])
    at = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    await guard_monitor(positions, dispatcher, at)
    intent = deepcopy(option["execution_route_trigger"])
    assert option["execution_route_pending"] is True
    assert record.status == "EXIT_PENDING"

    await main._monitor_completed_atr([], positions, dispatcher, current_time=at + timedelta(seconds=1))

    assert option["execution_route_pending"] is False
    assert option["execution_route_trigger"] == intent
    assert record.pending_exit_quantity == 65
    # A later premium trigger can be retained for reference accounting, but
    # cannot replace the first accepted execution route's index domain.
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await positions._close_option_premium_trade(
            trade, 99, at + timedelta(seconds=2), source="dhan_last_trade")
    assert option["execution_route_trigger"] == intent
    assert option["execution_route_pending"] is False
    assert option["execution_route_reason"] == "STOPPED_OUT_AT_BE"
