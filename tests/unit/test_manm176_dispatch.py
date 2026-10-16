"""Exercise signal-to-order-to-position flow through real paper execution."""
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from execution.order_manager import OrderManager
from execution.signal_dispatch import SignalDispatcher
from models import AresSignal, Direction, OptionGreeks, OptionOrderBracket, SetupType


def candidate():
    signal = AresSignal(
        setup_type=SetupType.EXHAUSTION_REVERSAL, direction=Direction.BULLISH,
        trigger_price=24000, entry_zone=(23995, 24005), stop_loss=23990,
        target_1=24020, target_2=24040, confidence="HIGH", reasons=[],
        timestamp=datetime(2026, 10, 16, 10), strike_to_trade=24000, option_type="CE",
    )
    signal.bracket = OptionOrderBracket(
        security_id="123", expiry_date="2099-01-01", strike=24000, option_type="CE",
        entry_limit=100, synthetic_spot_sl=23990, catastrophic_opt_sl=95,
        target_1=110, target_2=120, lots=2, greeks=OptionGreeks(.5,.001,-10,2,15),
        is_valid=True, signal_id=str(signal.id), spot_entry=24000,
        spot_target_1=24020, spot_target_2=24040,
    )
    return signal


def chain(ask=100, security_id="123", spot=24000):
    return [{"strike":24000,"expiry_date":"2099-01-01","ce_security_id":security_id,
             "ce_ask":ask,"ce_bid":ask-.05,"underlying_spot":spot}]


class Positions:
    def __init__(self):
        self.active_trades = []
        self.entries = []
    async def add_trade(self, signal, spot, atm=None):
        self.entries.append((signal, spot))
        self.active_trades.append({"id":"trade-1","signal_uuid":str(signal.id)})
        return "trade-1", "BOUND"


@pytest.mark.asyncio
async def test_limit_waits_and_registers_only_after_observed_fill():
    positions = Positions()
    dispatcher = SignalDispatcher(OrderManager(), positions)
    signal = candidate()
    assert not await dispatcher.dispatch(signal, 24000, chain(101))
    assert not positions.entries
    await dispatcher.on_snapshot(chain(99.95), 24001)
    assert len(positions.entries) == 1
    assert signal.trade_id == "trade-1"
    assert signal.market_context["execution"]["mode"] == "PAPER"
    await dispatcher.on_snapshot(chain(99.95), 24001)
    assert len(positions.entries) == 1


@pytest.mark.asyncio
async def test_invalid_bracket_never_registers():
    positions = Positions()
    dispatcher = SignalDispatcher(OrderManager(), positions)
    signal = candidate()
    signal.bracket = replace(signal.bracket,is_valid=False,lots=0,rejection_reason="INVALID_DATA")
    assert not await dispatcher.dispatch(signal, 24000, chain())
    assert not positions.entries


@pytest.mark.asyncio
async def test_tick_invalidation_cancels_pending_limit():
    positions = Positions()
    dispatcher = SignalDispatcher(OrderManager(), positions)
    signal = candidate()
    await dispatcher.dispatch(signal,24000,chain(101))
    await dispatcher.on_spot(23989)
    await dispatcher.on_snapshot(chain(99),24000)
    assert not positions.entries


@pytest.mark.asyncio
async def test_quote_identity_must_match_contract():
    positions = Positions()
    dispatcher = SignalDispatcher(OrderManager(), positions)
    await dispatcher.dispatch(candidate(),24000,chain(security_id="wrong"))
    assert not positions.entries


@pytest.mark.asyncio
async def test_registration_failure_can_retry_without_second_order_or_fill():
    positions = Positions()
    dispatcher = SignalDispatcher(OrderManager(), positions)
    original = positions.add_trade
    async def failing(*args,**kwargs):
        raise RuntimeError("database unavailable")
    positions.add_trade = failing
    signal = candidate()
    with pytest.raises(RuntimeError):
        await dispatcher.dispatch(signal,24000,chain())
    positions.add_trade = original
    await dispatcher.on_snapshot(chain(),24000)
    assert len(positions.entries) == 1
    assert len(dispatcher.orders.records) == 1

@pytest.mark.asyncio
async def test_pending_signals_are_deduplicated_before_any_fill():
    positions = Positions()
    dispatcher = SignalDispatcher(OrderManager(), positions)
    first, second = candidate(), candidate()
    await dispatcher.dispatch(first,24000,chain(101))
    assert not await dispatcher.dispatch(second,24000,chain(101))
    assert second.trade_binding_status == "DUPLICATE_SKIPPED"
    assert len(dispatcher.orders.records) == 1


@pytest.mark.asyncio
async def test_failed_registration_does_not_block_other_pending_orders():
    positions = Positions()
    dispatcher = SignalDispatcher(OrderManager(), positions)
    first, second = candidate(), candidate()
    second.setup_type = SetupType.OI_WALL_REJECTION
    await dispatcher.dispatch(first,24000,chain(101))
    await dispatcher.dispatch(second,24000,chain(101))
    original = positions.add_trade
    async def register(signal,*args,**kwargs):
        if signal is first:
            raise RuntimeError("one failed write")
        return await original(signal,*args,**kwargs)
    positions.add_trade = register
    await dispatcher.on_snapshot(chain(),24000)
    assert second.trade_id == "trade-1"


@pytest.mark.asyncio
async def test_execution_transitions_sync_to_persistence_boundary():
    positions = Positions()
    synced = []
    async def sync_execution(signal, execution):
        synced.append(dict(execution))
    positions.sync_execution = sync_execution
    dispatcher = SignalDispatcher(OrderManager(), positions)
    signal = candidate()
    await dispatcher.dispatch(signal,24000,chain())
    await dispatcher.on_snapshot(chain(110, spot=24020),24020)
    assert synced[-1]["exited_quantity"] == 65

@pytest.mark.asyncio
async def test_failed_telemetry_sync_preserves_successful_trade_binding():
    positions = Positions()
    async def fail_sync(*args):
        raise RuntimeError("telemetry unavailable")
    positions.sync_execution = fail_sync
    dispatcher = SignalDispatcher(OrderManager(),positions)
    signal = candidate()
    with pytest.raises(RuntimeError,match="telemetry unavailable"):
        await dispatcher.dispatch(signal,24000,chain())
    assert signal.trade_id == "trade-1"
    await dispatcher.on_snapshot(chain(),24001)
    assert len(positions.entries) == 1


def test_broker_rejection_on_init(tmp_path):
    broker = object()
    manager = OrderManager(broker=broker, allow_live=True, journal_path=tmp_path / "orders.json")
    with pytest.raises(ValueError, match="The application dispatcher currently supports paper execution only"):
        SignalDispatcher(manager, Positions())


@pytest.mark.asyncio
async def test_find_duplicate_trade_skipped():
    positions = Positions()
    positions.find_duplicate_trade = lambda signal, spot: True
    dispatcher = SignalDispatcher(OrderManager(), positions)
    signal = candidate()
    assert not await dispatcher.dispatch(signal, 24000, chain())
    assert signal.trade_binding_status == "DUPLICATE_SKIPPED"


@pytest.mark.asyncio
async def test_on_spot_exception_resilience():
    positions = Positions()
    manager = OrderManager()
    dispatcher = SignalDispatcher(manager, positions)
    signal = candidate()
    await dispatcher.dispatch(signal, 24000, chain(101))
    def failing_on_spot(*args, **kwargs):
        raise RuntimeError("Simulated spot processing failure")
    manager.on_spot = failing_on_spot
    await dispatcher.on_spot(24005)


@pytest.mark.asyncio
async def test_publish_skips_unmatched_active_trade():
    positions = Positions()
    positions.active_trades.append({"id": "other-trade", "signal_uuid": "some-other-uuid"})
    dispatcher = SignalDispatcher(OrderManager(), positions)
    signal = candidate()
    await dispatcher.dispatch(signal, 24000, chain())
    assert "execution" not in positions.active_trades[0].get("market_context", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("option_type,direction,fill_spot,target_spot", [
    ("CE", Direction.BULLISH, 24002, 24020),
    ("PE", Direction.BEARISH, 23998, 23980),
])
async def test_quote_spot_sets_entry_and_post_target_break_even(option_type, direction, fill_spot, target_spot):
    positions = Positions()
    orders = OrderManager()
    dispatcher = SignalDispatcher(orders, positions)
    signal = candidate()
    signal.direction = direction
    signal.bracket = replace(
        signal.bracket, option_type=option_type,
        synthetic_spot_sl=23990 if option_type == "CE" else 24010,
        spot_target_1=target_spot,
        spot_target_2=24040 if option_type == "CE" else 23960,
    )
    side = option_type.lower()
    row = {"strike":24000, "expiry_date":"2099-01-01", f"{side}_security_id":"123",
           f"{side}_ask":100, f"{side}_bid":99.95, "underlying_spot":fill_spot}
    assert await dispatcher.dispatch(signal, 24000, [row])
    record = next(iter(orders.records.values()))
    assert record.entry_spot == positions.entries[0][1] == fill_spot
    assert signal.market_context["execution"]["spot_at_fill"] == fill_spot
    row.update({"underlying_spot":target_spot, f"{side}_ask":110, f"{side}_bid":109.95})
    await dispatcher.on_snapshot([row], 24000)
    assert record.exited_quantity == 65
    assert record.active_spot_sl == fill_spot
    await dispatcher.on_spot(fill_spot)
    assert record.exit_reason == "STOPPED_OUT_AT_BE"


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_spot", [None, True, "24000", 0, -1, float("nan"), float("inf")])
async def test_invalid_quote_spot_cannot_fill_but_candle_still_invalidates(quote_spot):
    positions = Positions()
    orders = OrderManager()
    dispatcher = SignalDispatcher(orders, positions)
    signal = candidate()
    quotes = chain(99, spot=quote_spot)
    if quote_spot is None:
        quotes[0].pop("underlying_spot")
    assert not await dispatcher.dispatch(signal, 24000, quotes)
    record = next(iter(orders.records.values()))
    assert record.status == "SUBMITTED"
    await dispatcher.on_snapshot(quotes, 23989)
    assert record.status == "CANCELLED"
    assert not positions.entries


@pytest.mark.asyncio
async def test_registration_retries_after_fill_has_closed_preserving_original_fill():
    positions = Positions()
    dispatcher = SignalDispatcher(OrderManager(), positions)
    original = positions.add_trade
    async def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")
    positions.add_trade = fail
    signal = candidate()
    with pytest.raises(RuntimeError):
        await dispatcher.dispatch(signal, 24000, chain(99, spot=24002))
    filled_at = signal.market_context["execution"]["filled_at"]
    await dispatcher.on_snapshot(chain(94, spot=24003), 24001)
    assert signal.market_context["execution"]["status"] == "CLOSED"
    positions.add_trade = original
    await dispatcher.on_spot(24004)
    assert len(positions.entries) == 1
    assert positions.entries[0][1] == 24002
    assert signal.market_context["execution"]["filled_at"] == filled_at
    assert signal.market_context["execution"]["realized_pnl"] == pytest.approx(-656.5)
    await dispatcher.on_snapshot(chain(), 24000)
    assert len(positions.entries) == 1


@pytest.mark.asyncio
async def test_closed_trade_waiting_for_ml_snapshot_does_not_block_new_signal():
    positions = Positions()
    async def waiting_for_snapshot(*args):
        return False
    positions.sync_execution = waiting_for_snapshot
    dispatcher = SignalDispatcher(OrderManager(), positions)
    first, second = candidate(), candidate()
    quotes = chain(99)
    quotes[0]["ce_bid"] = 94
    assert await dispatcher.dispatch(first, 24000, quotes)
    assert first.market_context["execution"]["status"] == "CLOSED"
    assert await dispatcher.dispatch(second, 24000, chain())
    assert len(positions.entries) == 2
