"""No-new-bar polls must preserve live tick monitoring without fabricating data."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import main


@pytest.mark.asyncio
async def test_no_completed_bar_keeps_tick_exits_and_pending_orders_running(monkeypatch):
    engine = MagicMock()
    price = MagicMock()
    price.fetch_latest_candle = AsyncMock(return_value=None)
    price.fetch_previous_day_ohlc = AsyncMock(return_value=(23149.85, 22913.80))
    oi = MagicMock()
    oi.fetch_chain = AsyncMock()
    positions = MagicMock()
    positions.active_trades = [{"id": "existing-paper-trade"}]
    positions.update_trades = AsyncMock(return_value=[])
    dispatcher = MagicMock()
    dispatcher.on_spot = AsyncMock()
    collector = MagicMock()
    collector.check_table_exists.return_value = False
    collector.snapshot = AsyncMock()
    feed = MagicMock()
    feed.is_active = True
    feed.get_latest_price.return_value = 22900.0
    settings = MagicMock()
    settings.poll_interval_seconds = 2
    settings.tick_exit_check_interval_seconds = 1
    clock = MagicMock()
    # A normal market poll followed by session close stops the real run loop.
    clock.now.side_effect = [datetime(2026, 10, 19, 10), datetime(2026, 10, 19, 15, 30)]
    errors = AsyncMock()

    for name, instance in {
        "AresEngine": engine, "PriceFetcher": price, "OIFetcher": oi,
        "LevelFetcher": MagicMock(), "Storage": MagicMock(),
        "PositionManager": positions, "SignalDispatcher": dispatcher,
        "OrderManager": MagicMock(), "MLCollector": collector,
        "TickFeed": feed, "SignalPredictor": MagicMock(),
        "PredictionLogger": MagicMock(),
    }.items():
        monkeypatch.setattr(main, name, MagicMock(return_value=instance))
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "datetime", clock)
    monkeypatch.setattr(main, "_session_now", lambda: datetime(2026, 10, 19, 10))
    monkeypatch.setattr(main, "load_dhan_credentials_from_supabase", MagicMock())
    monkeypatch.setattr(main, "is_expiry_day_from_api", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "send_startup_alert", AsyncMock())
    monkeypatch.setattr(main, "send_error_alert", errors)
    monkeypatch.setattr(main, "initialize_oi_watch_outbox", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "print_banner", MagicMock())
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())

    await main.run()

    assert price.fetch_latest_candle.await_count == 2
    errors.assert_not_awaited()
    engine.tick.assert_not_called()
    oi.fetch_chain.assert_not_awaited()
    collector.snapshot.assert_not_awaited()
    # Exercise the actual sleep helper: existing exits and pending orders still
    # receive live prices throughout the two-second no-new-bar polling pause.
    assert positions.update_trades.await_count == 2
    positions.update_trades.assert_awaited_with(22900.0)
    assert dispatcher.on_spot.await_count == 2
    dispatcher.on_spot.assert_awaited_with(22900.0)
