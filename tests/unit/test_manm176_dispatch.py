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


def chain(ask=100, security_id="123"):
    return [{"strike":24000,"expiry_date":"2099-01-01","ce_security_id":security_id,
             "ce_ask":ask,"ce_bid":ask-.05}]


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
    await dispatcher.on_snapshot(chain(110),24020)
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
