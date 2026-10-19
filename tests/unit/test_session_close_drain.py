"""Exercise no-new-bar monitoring and final-bar draining through the run loop."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from models import AresSignal, Direction, OHLCVCandle, SetupType


def candle(hour=15, minute=28, day=19):
    return OHLCVCandle(datetime(2026, 10, day, hour, minute), 24000, 24020, 23980, 24000, 100, 24000)


@pytest.fixture
def loop(monkeypatch):
    def install(candles, times, signals=None):
        engine = MagicMock()
        engine.tick.side_effect = signals or [None] * len(candles)
        engine.candle_buffer = []
        price = MagicMock()
        price.fetch_latest_candle = AsyncMock(side_effect=candles)
        price.fetch_latest_spot = AsyncMock(return_value=24000.0)
        price.fetch_previous_day_ohlc = AsyncMock(return_value=(24100.0, 23900.0))
        oi = MagicMock()
        oi.get_nearest_expiry = AsyncMock(return_value="2099-01-01")
        oi.fetch_chain = AsyncMock(return_value=(None, []))
        positions = MagicMock()
        positions.update_trades = AsyncMock(return_value=[])
        dispatcher = MagicMock()
        dispatcher.on_snapshot = AsyncMock()
        dispatcher.on_spot = AsyncMock()
        dispatcher.dispatch = AsyncMock()
        collector = MagicMock()
        collector.check_table_exists.return_value = False
        collector.snapshot = AsyncMock()
        collector.stats = {"total_snapshots": 0, "signals_recorded": 0}
        feed = MagicMock(is_active=False)
        settings = MagicMock(poll_interval_seconds=1, tick_exit_check_interval_seconds=1, candle_buffer_size=10)
        clock = MagicMock()
        clock.now.side_effect = times
        storage = MagicMock()
        storage.log_signal = AsyncMock(return_value=True)
        predictor = MagicMock()
        predictor.load_model.side_effect = ValueError("no model")
        errors, sizing, alerts, watches = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
        outbox = MagicMock()
        outbox.close = AsyncMock()
        for name, instance in {
            "AresEngine": engine, "PriceFetcher": price, "OIFetcher": oi,
            "LevelFetcher": MagicMock(), "Storage": storage,
            "PositionManager": positions, "SignalDispatcher": dispatcher,
            "OrderManager": MagicMock(), "MLCollector": collector,
            "TickFeed": feed, "SignalPredictor": predictor, "PredictionLogger": MagicMock(),
        }.items():
            monkeypatch.setattr(main, name, MagicMock(return_value=instance))
        for name, value in {
            "settings": settings, "datetime": clock,
            "_session_now": MagicMock(return_value=times[0]),
            "load_dhan_credentials_from_supabase": MagicMock(),
            "is_expiry_day_from_api": AsyncMock(return_value=False),
            "send_startup_alert": AsyncMock(), "send_error_alert": errors,
            "send_discord": alerts, "process_options_calculation": sizing,
            "dispatch_oi_wall_watch_alerts": watches,
            "initialize_oi_watch_outbox": AsyncMock(return_value=outbox),
            "print_banner": MagicMock(), "format_signal_console": MagicMock(),
        }.items():
            monkeypatch.setattr(main, name, value)
        monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
        return SimpleNamespace(**locals())
    return install


@pytest.mark.asyncio
async def test_final_bar_reaches_detector_snapshot_and_intrabar_exits(loop):
    first, final = candle(), candle(minute=29)
    ctx = loop([first, final], [datetime(2026, 10, 19, 15, 29), datetime(2026, 10, 19, 15, 30)])
    await main.run()
    assert [call.args[0] for call in ctx.engine.tick.call_args_list] == [first, final]
    assert ctx.collector.snapshot.call_args.kwargs["candle"] is final
    ctx.positions.update_trades.assert_awaited_with(final.close, candle_high=final.high,
                                                  candle_low=final.low, candle_timestamp=final.timestamp)
    assert ctx.dispatcher.on_snapshot.await_count == 1
    ctx.outbox.close.assert_awaited_once()
    ctx.feed.stop.assert_called()


@pytest.mark.asyncio
async def test_final_candidate_is_analysis_without_sizing_or_new_order(loop):
    signal = AresSignal(SetupType.TREND_CONTINUATION, Direction.BULLISH, 24000,
                        (23995, 24005), 23990, 24020, 24040, "MEDIUM", [],
                        datetime(2026, 10, 19, 15, 29), 24000, "CE")
    ctx = loop([candle(), candle(minute=29)],
               [datetime(2026, 10, 19, 15, 29), datetime(2026, 10, 19, 15, 30)], [None, signal])
    await main.run()
    assert signal.options_sizing_status == "SESSION_CLOSED"
    assert signal.trade_binding_status == "SESSION_CLOSED"
    assert signal.suggested_lots == 0 and signal.bracket is None
    assert signal.market_context["options_sizing"]["rejection_reason"] == "SESSION_CLOSED"
    assert any("analysis" in reason.lower() and "closed" in reason.lower() for reason in signal.reasons)
    ctx.storage.log_signal.assert_awaited_once_with(signal, 24000)
    ctx.sizing.assert_not_awaited()
    ctx.dispatcher.dispatch.assert_not_awaited()
    ctx.alerts.assert_not_awaited()
    assert ctx.collector.snapshot.call_args.kwargs["signal"] is signal
    assert ctx.watches.await_count == 2  # Normal poll plus terminal cleanup.


@pytest.mark.parametrize("final", [None, ValueError("provider malformed"), candle(), candle(hour=9, minute=15), candle(minute=29, day=16)])
@pytest.mark.asyncio
async def test_final_attempt_is_bounded_and_ignores_unavailable_or_stale_bar(loop, final):
    ctx = loop([candle(), final], [datetime(2026, 10, 19, 15, 29), datetime(2026, 10, 19, 15, 30)])
    await main.run()
    assert ctx.price.fetch_latest_candle.await_count == 2
    assert ctx.engine.tick.call_count == 1
    ctx.outbox.close.assert_awaited_once()
    ctx.feed.stop.assert_called()


@pytest.mark.parametrize("at", [datetime(2026, 10, 19, 15, 30), datetime(2026, 10, 19, 16)])
@pytest.mark.asyncio
async def test_after_hours_cold_start_never_replays_session(loop, at):
    ctx = loop([], [at])
    await main.run()
    ctx.price.fetch_latest_candle.assert_not_awaited()
    ctx.engine.tick.assert_not_called()
    ctx.outbox.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_new_bar_gets_fresh_rest_exit_and_option_quote_without_detector(loop):
    ctx = loop([None], [datetime(2026, 10, 19, 10), datetime(2026, 10, 19, 15, 30)])
    await main.run()
    ctx.price.fetch_latest_spot.assert_awaited_once()
    ctx.positions.update_trades.assert_awaited_once_with(24000.0)
    ctx.dispatcher.on_snapshot.assert_awaited_once_with([], 24000.0)
    ctx.engine.tick.assert_not_called()
    ctx.collector.snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_rest_exit_survives_chain_failure_and_clears_cooldown(loop):
    ctx = loop([None], [datetime(2026, 10, 19, 10), datetime(2026, 10, 19, 15, 30)])
    ctx.price.fetch_latest_spot.return_value = 23989.0
    ctx.positions.update_trades.return_value = [("trade", "SL_HIT")]
    async def unavailable(*args, **kwargs):
        ctx.positions.update_trades.assert_awaited_once_with(23989.0)
        raise ValueError("option chain unavailable")
    ctx.oi.fetch_chain.side_effect = unavailable
    await main.run()
    ctx.positions.update_trades.assert_awaited_once_with(23989.0)
    ctx.engine.clear_cooldown.assert_called_once()
    ctx.dispatcher.on_spot.assert_awaited_once_with(23989.0)
    ctx.dispatcher.on_snapshot.assert_not_awaited()
    ctx.engine.tick.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_crossing_close_never_fills_pending_quote_or_enters(loop, monkeypatch):
    signal = AresSignal(SetupType.TREND_CONTINUATION, Direction.BULLISH, 24000,
                        (23995, 24005), 23990, 24020, 24040, "MEDIUM", [],
                        datetime(2026, 10, 19, 15, 29), 24000, "CE")
    ctx = loop([candle(minute=29)],
               [datetime(2026, 10, 19, 15, 29, 59), datetime(2026, 10, 19, 15, 30)], [signal])
    monkeypatch.setattr(main, "_session_now", lambda: datetime(2026, 10, 19, 15, 30))
    await main.run()
    assert signal.trade_binding_status == "SESSION_CLOSED"
    ctx.sizing.assert_not_awaited()
    ctx.dispatcher.on_snapshot.assert_not_awaited()
    ctx.dispatcher.dispatch.assert_not_awaited()
    ctx.alerts.assert_not_awaited()
    ctx.positions.update_trades.assert_awaited_once()


@pytest.mark.asyncio
async def test_final_chain_failure_keeps_final_candle_exit_checks(loop):
    ctx = loop([candle(), candle(minute=29)],
               [datetime(2026, 10, 19, 15, 29), datetime(2026, 10, 19, 15, 30)])
    ctx.oi.fetch_chain.side_effect = [(None, []), ValueError("chain unavailable")]
    await main.run()
    final = candle(minute=29)
    ctx.positions.update_trades.assert_awaited_with(final.close, candle_high=final.high,
                                                  candle_low=final.low, candle_timestamp=final.timestamp)
    ctx.outbox.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_hanging_final_fetch_is_cancelled_and_cleanup_still_runs(loop, monkeypatch):
    import asyncio
    ctx = loop([candle()], [datetime(2026, 10, 19, 15, 29), datetime(2026, 10, 19, 15, 30)])
    cancelled = []
    calls = []
    async def fetch():
        calls.append(True)
        if len(calls) == 1:
            return candle()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    ctx.price.fetch_latest_candle.side_effect = fetch
    original_wait_for = asyncio.wait_for
    async def short_wait_for(awaitable, timeout):
        return await original_wait_for(awaitable, timeout=.01)
    monkeypatch.setattr(main.asyncio, "wait_for", short_wait_for)
    await main.run()
    assert calls == [True, True] and cancelled == [True]
    assert ctx.engine.tick.call_count == 1
    ctx.outbox.close.assert_awaited_once()
    ctx.feed.stop.assert_called()


@pytest.mark.asyncio
async def test_rest_poll_updates_real_pending_paper_order_without_new_detector_bar(loop, monkeypatch):
    from execution.order_manager import OrderManager
    from execution.signal_dispatch import SignalDispatcher
    from models import OptionGreeks, OptionOrderBracket
    signal = AresSignal(SetupType.TREND_CONTINUATION, Direction.BULLISH, 24000,
                        (23995, 24005), 23990, 24020, 24040, "MEDIUM", [],
                        datetime(2026, 10, 19, 10), 24000, "CE")
    signal.bracket = OptionOrderBracket(
        security_id="123", expiry_date="2099-01-01", strike=24000, option_type="CE",
        entry_limit=100, synthetic_spot_sl=23990, catastrophic_opt_sl=95,
        target_1=110, target_2=120, lots=1, greeks=OptionGreeks(.5, .001, -10, 2, 15),
        is_valid=True, signal_id=signal.id, spot_entry=24000,
        spot_target_1=24020, spot_target_2=24040,
    )
    def chain(ask):
        return [{"strike": 24000, "expiry_date": "2099-01-01", "ce_security_id": "123",
                 "ce_ask": ask, "ce_bid": ask - .05, "underlying_spot": 24000}]
    ctx = loop([None], [datetime(2026, 10, 19, 10), datetime(2026, 10, 19, 15, 30)])
    ctx.positions.find_duplicate_trade.return_value = False
    ctx.positions.active_trades = []
    ctx.positions.add_trade = AsyncMock(return_value=("pending-trade", "BOUND"))
    ctx.positions.sync_execution = AsyncMock(return_value=True)
    dispatcher = SignalDispatcher(OrderManager(), ctx.positions)
    await dispatcher.dispatch(signal, 24000, chain(101))
    ctx.positions.add_trade.assert_not_awaited()
    monkeypatch.setattr(main, "SignalDispatcher", MagicMock(return_value=dispatcher))
    ctx.oi.fetch_chain.return_value = (None, chain(99.95))
    await main.run()
    assert signal.trade_id == "pending-trade"
    ctx.positions.add_trade.assert_awaited_once_with(signal, 24000, atm=None)
    ctx.engine.tick.assert_not_called()
    ctx.collector.snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_outbox_close_failure_still_stops_tick_feed(loop):
    ctx = loop([], [datetime(2026, 10, 19, 15, 30)])
    ctx.outbox.close.side_effect = RuntimeError("outbox close failed")
    with pytest.raises(RuntimeError, match="outbox close failed"):
        await main.run()
    ctx.feed.stop.assert_called_once()


@pytest.mark.asyncio
async def test_terminal_watch_failure_still_closes_outbox_and_stops_feed(loop):
    ctx = loop([candle(), candle(minute=29)],
               [datetime(2026, 10, 19, 15, 29), datetime(2026, 10, 19, 15, 30)])
    ctx.watches.side_effect = [None, RuntimeError("terminal watch cleanup failed")]
    with pytest.raises(RuntimeError, match="terminal watch cleanup failed"):
        await main.run()
    ctx.outbox.close.assert_awaited_once()
    ctx.feed.stop.assert_called_once()


@pytest.mark.asyncio
async def test_prior_bar_request_crossing_close_drains_final_bar_without_poll_sleep(loop, monkeypatch):
    first, final = candle(), candle(minute=29)
    ctx = loop([first, final], [datetime(2026, 10, 19, 15, 29, 59), datetime(2026, 10, 19, 15, 30, 2)])
    monkeypatch.setattr(main, "_session_now", lambda: datetime(2026, 10, 19, 15, 30, 2))
    await main.run()
    assert [call.args[0] for call in ctx.engine.tick.call_args_list] == [first, final]
    main.asyncio.sleep.assert_not_awaited()
    ctx.dispatcher.on_snapshot.assert_not_awaited()
