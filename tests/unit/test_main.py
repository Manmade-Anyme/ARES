"""
Tests for TASK-173 audit P2 item 18: main.py's tick-driven exit sleep helper.

`_sleep_with_tick_exits` replaces the plain 60s `asyncio.sleep` between REST
poll cycles. When the WebSocket TickFeed is active, it wakes every
`tick_exit_check_interval_seconds` to run a tick-driven exit check
(position_manager.update_trades against the latest WS LTP) so SL/T1/T2 hits
are caught between candle closes, not just at the 60s boundary. Falls back
to a single plain sleep when the feed isn't active — zero behavior change
from the pre-TASK-173 loop in that case.
"""
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

import main


class TestSleepWithTickExits(unittest.IsolatedAsyncioTestCase):

    async def test_signal_snapshot_is_durable_before_cycle_continues(self):
        collector = MagicMock()
        insert_finished = asyncio.get_running_loop().create_future()
        collector.snapshot.return_value = insert_finished

        task = asyncio.create_task(main._record_ml_snapshot(collector, object(), spot=24000.0))
        await asyncio.sleep(0)

        self.assertFalse(task.done())
        insert_finished.set_result(None)
        await task
        collector.snapshot.assert_called_once()

    async def test_non_signal_snapshot_does_not_block_cycle(self):
        collector = MagicMock()
        insert_finished = asyncio.get_running_loop().create_future()
        collector.snapshot.return_value = insert_finished

        await main._record_ml_snapshot(collector, None, spot=24000.0)

        self.assertFalse(insert_finished.done())
        insert_finished.cancel()

    async def test_record_ml_snapshot_catches_exception(self):
        # 1. Sync exception from snapshot() call
        collector = MagicMock()
        collector.snapshot = MagicMock(side_effect=Exception("sync error"))
        with patch("builtins.print") as mock_print:
            await main._record_ml_snapshot(collector, object(), spot=24000.0)
            mock_print.assert_called_with("[-] _record_ml_snapshot failed: sync error")

        # 2. Async exception from awaiting insert_coro
        collector = MagicMock()
        collector.snapshot.return_value = AsyncMock(side_effect=Exception("async error"))()
        with patch("builtins.print") as mock_print:
            await main._record_ml_snapshot(collector, object(), spot=24000.0)
            mock_print.assert_called_with("[-] _record_ml_snapshot failed: async error")

    async def test_inactive_feed_sleeps_once_for_full_duration(self):
        tick_feed = MagicMock()
        tick_feed.is_active = False
        position_manager = MagicMock()
        engine = MagicMock()

        with patch('main.asyncio.sleep', new=AsyncMock()) as mock_sleep:
            await main._sleep_with_tick_exits(60, tick_feed, position_manager, engine)

        mock_sleep.assert_awaited_once_with(60)
        position_manager.update_trades.assert_not_called()

    async def test_active_feed_checks_exits_at_each_interval(self):
        tick_feed = MagicMock()
        tick_feed.is_active = True
        tick_feed.get_latest_price.return_value = 24000.0
        position_manager = MagicMock()
        position_manager.active_trades = [{"id": "t1"}]
        position_manager.update_trades = AsyncMock(return_value=[])
        engine = MagicMock()

        with patch('main.asyncio.sleep', new=AsyncMock()) as mock_sleep, \
             patch('main.settings') as mock_settings:
            mock_settings.tick_exit_check_interval_seconds = 2.0
            await main._sleep_with_tick_exits(6, tick_feed, position_manager, engine)

        self.assertEqual(mock_sleep.await_count, 3)  # 6s / 2s steps
        self.assertEqual(position_manager.update_trades.await_count, 3)
        position_manager.update_trades.assert_awaited_with(24000.0)

    async def test_sl_hit_clears_engine_cooldown(self):
        tick_feed = MagicMock()
        tick_feed.is_active = True
        tick_feed.get_latest_price.return_value = 23900.0
        position_manager = MagicMock()
        position_manager.active_trades = [{"id": "t1"}]
        position_manager.update_trades = AsyncMock(return_value=[("t1", "SL_HIT")])
        engine = MagicMock()

        with patch('main.asyncio.sleep', new=AsyncMock()), \
             patch('main.settings') as mock_settings:
            mock_settings.tick_exit_check_interval_seconds = 2.0
            await main._sleep_with_tick_exits(2, tick_feed, position_manager, engine)

        engine.clear_cooldown.assert_called_once()

    async def test_skips_update_when_no_active_trades(self):
        tick_feed = MagicMock()
        tick_feed.is_active = True
        tick_feed.get_latest_price.return_value = 24000.0
        position_manager = MagicMock()
        position_manager.active_trades = []
        position_manager.update_trades = AsyncMock(return_value=[])
        engine = MagicMock()

        with patch('main.asyncio.sleep', new=AsyncMock()), \
             patch('main.settings') as mock_settings:
            mock_settings.tick_exit_check_interval_seconds = 2.0
            await main._sleep_with_tick_exits(2, tick_feed, position_manager, engine)

        position_manager.update_trades.assert_not_called()

    async def test_skips_update_when_price_unavailable(self):
        tick_feed = MagicMock()
        tick_feed.is_active = True
        tick_feed.get_latest_price.return_value = None
        position_manager = MagicMock()
        position_manager.active_trades = [{"id": "t1"}]
        position_manager.update_trades = AsyncMock(return_value=[])
        engine = MagicMock()

        with patch('main.asyncio.sleep', new=AsyncMock()), \
             patch('main.settings') as mock_settings:
            mock_settings.tick_exit_check_interval_seconds = 2.0
            await main._sleep_with_tick_exits(2, tick_feed, position_manager, engine)

        position_manager.update_trades.assert_not_called()

    async def test_update_trades_exception_is_caught(self):
        tick_feed = MagicMock()
        tick_feed.is_active = True
        tick_feed.get_latest_price.return_value = 24000.0
        position_manager = MagicMock()
        position_manager.active_trades = [{"id": "t1"}]
        position_manager.update_trades = AsyncMock(side_effect=Exception("db down"))
        engine = MagicMock()

        with patch('main.asyncio.sleep', new=AsyncMock()), \
             patch('main.settings') as mock_settings:
            mock_settings.tick_exit_check_interval_seconds = 2.0
            await main._sleep_with_tick_exits(2, tick_feed, position_manager, engine)  # must not raise

        engine.clear_cooldown.assert_not_called()

    async def test_final_partial_step_is_clamped(self):
        """5s total with a 2s interval steps 2,2,1 -> 3 sleeps summing to 5,
        never overshooting the REST poll cadence."""
        tick_feed = MagicMock()
        tick_feed.is_active = True
        tick_feed.get_latest_price.return_value = 24000.0
        position_manager = MagicMock()
        position_manager.active_trades = [{"id": "t1"}]
        position_manager.update_trades = AsyncMock(return_value=[])
        engine = MagicMock()

        with patch('main.asyncio.sleep', new=AsyncMock()) as mock_sleep, \
             patch('main.settings') as mock_settings:
            mock_settings.tick_exit_check_interval_seconds = 2.0
            await main._sleep_with_tick_exits(5, tick_feed, position_manager, engine)

        called_with = [c.args[0] for c in mock_sleep.await_args_list]
        self.assertEqual(called_with, [2.0, 2.0, 1.0])


    @patch("main.AresEngine")
    @patch("main.PriceFetcher")
    @patch("main.OIFetcher")
    @patch("main.LevelFetcher")
    @patch("main.Storage")
    @patch("main.PositionManager")
    @patch("main.MLCollector")
    @patch("main.TickFeed")
    @patch("main.SignalPredictor")
    @patch("main.is_expiry_day_from_api", return_value=False)
    @patch("main.load_dhan_credentials_from_supabase")
    @patch("main.asyncio.sleep", side_effect=Exception("StopLoop"))
    async def test_main_loop_passes_candle_timestamp(
        self, mock_sleep, mock_load, mock_is_expiry,
        MockPredictor, MockFeed, MockML, MockPM, MockStorage, MockLevel, MockOI, MockPrice, MockEngine
    ):
        pm_instance = MockPM.return_value
        pm_instance.update_trades = AsyncMock(return_value=[])
        
        price_instance = MockPrice.return_value
        import types
        from datetime import datetime, timezone
        fake_candle = types.SimpleNamespace(timestamp=datetime(2026, 9, 1, 10, 5, 0, tzinfo=timezone.utc),
                       open=24000.0, high=24010.0, low=23990.0, close=24000.0, volume=100)
        price_instance.fetch_latest_candle = AsyncMock(return_value=fake_candle)
        
        oi_instance = MockOI.return_value
        oi_instance.get_nearest_expiry = AsyncMock(return_value="2026-09-01")
        mock_atm = MagicMock()
        mock_atm.ce.iv = 15.0
        oi_instance.fetch_chain = AsyncMock(return_value=(mock_atm, MagicMock()))
        
        engine_instance = MockEngine.return_value
        engine_instance.is_cooldown = False
        engine_instance.tick.return_value = None
        
        with patch("main.settings") as mock_settings, \
             patch("main.datetime") as mock_dt:
            
            mock_dt.now.return_value = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            
            mock_settings.trading_start_time = "00:00"
            mock_settings.trading_end_time = "23:59"
            mock_settings.system_mode = "LIVE"
            mock_settings.tick_exit_check_interval_seconds = 2.0
            mock_settings.signal_cooldown_minutes = 5.0
            mock_settings.yahoo_symbol = "^NSEI"
            mock_settings.poll_interval_seconds = 60.0
            try:
                await main.run()
            except Exception as e:
                if str(e) != "StopLoop":
                    raise
        
        pm_instance.update_trades.assert_awaited_with(
            24000.0,
            candle_high=24010.0,
            candle_low=23990.0,
            candle_timestamp=fake_candle.timestamp
        )
