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
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

import main


class TestSleepWithTickExits(unittest.IsolatedAsyncioTestCase):

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


if __name__ == "__main__":
    unittest.main()
