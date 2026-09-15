"""MANM-151: durable active-trade writes must follow transition order."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import position_manager
from models import AresSignal, Direction, SetupType
from tests.unit.test_task194_ml_labels_and_oi_distribution import _FakeSupabase


class DelayedExecutor:
    """Let the test finish executor jobs in the worst possible order."""

    def __init__(self, loop):
        self.loop = loop
        self.jobs = []

    def submit(self, executor, function):
        future = self.loop.create_future()
        self.jobs.append((function, future))
        return future

    def complete_latest(self):
        function, future = self.jobs.pop()
        try:
            result = function()
        except Exception as error:
            future.set_exception(error)
        else:
            future.set_result(result)

    async def drain(self):
        # Allow newly unblocked per-trade continuations to submit their jobs.
        for _ in range(10):
            await asyncio.sleep(0)
            while self.jobs:
                self.complete_latest()


def make_signal(direction):
    bullish = direction == Direction.BULLISH
    return AresSignal(
        setup_type=SetupType.OI_WALL_REJECTION, direction=direction,
        trigger_price=24000.0, entry_zone=(23990.0, 24010.0),
        stop_loss=23975.0 if bullish else 24025.0,
        target_1=24050.0 if bullish else 23950.0,
        target_2=24100.0 if bullish else 23900.0,
        confidence="HIGH", reasons=["Ordering regression"],
        timestamp=datetime.now(timezone.utc), strike_to_trade=24000,
        option_type="CE" if bullish else "PE",
    )


@pytest_asyncio.fixture
async def manager(monkeypatch):
    sb = _FakeSupabase({"active_trades": [], "trade_analytics": []})
    monkeypatch.setattr(position_manager, "create_client", lambda *_: sb)
    monkeypatch.setattr(position_manager, "AnalyticsLogger", MagicMock)
    monkeypatch.setattr(position_manager, "send_trade_update", AsyncMock())
    pm = position_manager.PositionManager()
    executor = DelayedExecutor(asyncio.get_running_loop())
    monkeypatch.setattr(executor.loop, "run_in_executor", executor.submit)
    return pm, sb, executor


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [Direction.BULLISH, Direction.BEARISH])
@pytest.mark.parametrize("delayed_write", ["insert", "t1"])
@pytest.mark.parametrize("exit_type", ["T2_HIT", "SL_HIT"])
async def test_terminal_state_survives_delayed_earlier_write(
    manager, direction, delayed_write, exit_type
):
    pm, sb, executor = manager
    signal = make_signal(direction)

    pm.add_trade(signal, 24000.0)
    if delayed_write == "t1":
        await executor.drain()
        await pm.update_trades(signal.target_1)
        await asyncio.sleep(0)

    price = signal.target_2 if exit_type == "T2_HIT" else pm.active_trades[0]["stop_loss"]
    expected_exit = "STOPPED_OUT_AT_BE" if delayed_write == "t1" and exit_type == "SL_HIT" else exit_type
    events = await pm.update_trades(price)
    await executor.drain()

    trade = sb.rows["active_trades"][0]
    assert trade["state"] == "CLOSED"
    assert trade["exit_type"] == expected_exit
    assert trade["exit_price"] == price
    assert trade["exit_timestamp"]
    if expected_exit == "STOPPED_OUT_AT_BE":
        assert trade["pnl_points_override"] == 50.0
    assert events == [(trade["id"], expected_exit)]
    assert trade["stop_loss"] == (24000.0 if delayed_write == "t1" else signal.stop_loss)
    assert pm._trade_writes == {}

    restarted = position_manager.PositionManager()
    assert restarted.is_initialized
    assert restarted.active_trades == []
    assert await restarted.update_trades(price) == []


@pytest.mark.asyncio
async def test_delayed_insert_uses_original_payload(manager):
    pm, sb, executor = manager
    signal = make_signal(Direction.BULLISH)
    pm.add_trade(signal, 24000.0)
    await pm.update_trades(signal.target_1)
    await asyncio.sleep(0)

    executor.complete_latest()
    assert sb.rows["active_trades"][0]["state"] == "OPEN"
    assert sb.rows["active_trades"][0]["stop_loss"] == signal.stop_loss
    await executor.drain()
    assert sb.rows["active_trades"][0]["state"] == "T1_HIT"


@pytest.mark.asyncio
async def test_delayed_trade_does_not_block_another_trade(manager):
    pm, sb, executor = manager
    signal = make_signal(Direction.BULLISH)
    pm.add_trade(signal, 24000.0)
    pm.add_trade(signal, 24020.0)
    second_id = pm.active_trades[1]["id"]
    await asyncio.sleep(0)
    executor.complete_latest()  # Second insert finishes; first is still delayed.
    await asyncio.sleep(0)
    await pm.update_trades(signal.target_2)
    await asyncio.sleep(0)
    executor.complete_latest()

    assert len(sb.rows["active_trades"]) == 1
    assert sb.rows["active_trades"][0]["id"] == second_id
    assert sb.rows["active_trades"][0]["state"] == "CLOSED"
    await executor.drain()
    assert len(sb.rows["active_trades"]) == 2
    assert all(t["state"] == "CLOSED" for t in sb.rows["active_trades"])
    assert pm._trade_writes == {}


@pytest.mark.asyncio
async def test_failed_t1_write_is_reported_and_does_not_block_terminal_write(
    manager, monkeypatch, capsys
):
    pm, sb, executor = manager
    signal = make_signal(Direction.BULLISH)
    pm.add_trade(signal, 24000.0)
    await executor.drain()
    original_table = sb.table

    def table(name):
        query = original_table(name)
        original_update = query.update

        def update(payload):
            if payload.get("state") == "T1_HIT":
                raise RuntimeError("T1 persistence unavailable")
            return original_update(payload)

        query.update = update
        return query

    monkeypatch.setattr(sb, "table", table)
    await pm.update_trades(signal.target_1)
    await pm.update_trades(signal.target_2)
    await executor.drain()

    assert sb.rows["active_trades"][0]["state"] == "CLOSED"
    assert sb.rows["active_trades"][0]["exit_type"] == "T2_HIT"
    assert "T1 persistence unavailable" in capsys.readouterr().out
    assert pm._trade_writes == {}
