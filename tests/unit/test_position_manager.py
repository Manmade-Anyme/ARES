import unittest
from unittest.mock import MagicMock, patch, AsyncMock
import sys
import importlib
import asyncio
from datetime import datetime, timezone, timedelta
from models import AresSignal, SetupType, Direction

class MockSupabaseClient:
    def __init__(self):
        self.table_mock = MagicMock()
        self.last_table = None
        
        # Mock inserts
        self.insert_mock = MagicMock()
        self.table_mock.insert = self.insert_mock
        self.insert_mock.return_value.execute = MagicMock()
        
        # Mock updates
        self.update_mock = MagicMock()
        self.table_mock.update = self.update_mock
        self.update_mock.return_value.eq.return_value.execute = MagicMock()
        
        # Mock deletes
        self.delete_mock = MagicMock()
        self.table_mock.delete = self.delete_mock
        self.delete_mock.return_value.eq.return_value.execute = MagicMock()

        # Mock selects
        self.select_mock = MagicMock()
        self.table_mock.select = self.select_mock
        self.execute_mock = MagicMock()
        self.select_mock.return_value.execute = self.execute_mock
        self.execute_mock.return_value.data = []

    def table(self, name):
        self.last_table = name
        return self.table_mock

global_mock_client = MockSupabaseClient()
mock_create_patch = patch('supabase.create_client', return_value=global_mock_client)
mock_create_patch.start()

if 'position_manager' in sys.modules:
    importlib.reload(sys.modules['position_manager'])
else:
    import position_manager

from position_manager import PositionManager

class TestPositionManager(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_client = global_mock_client
        self.mock_client.insert_mock.reset_mock()
        self.mock_client.update_mock.reset_mock()
        self.mock_client.select_mock.reset_mock()
        self.mock_client.delete_mock.reset_mock()
        self.mock_client.insert_mock.side_effect = None
        self.mock_client.update_mock.side_effect = None
        self.mock_client.select_mock.side_effect = None
        self.mock_client.delete_mock.side_effect = None
        self.mock_client.execute_mock.side_effect = None
        self.mock_client.execute_mock.return_value.data = []
        self.mock_client.last_table = None

    @patch('position_manager.settings')
    def test_lazy_initialization_success(self, mock_settings):
        mock_settings.supabase_url = "https://mock.supabase.co"
        mock_settings.supabase_key = "key"
        
        self.mock_client.execute_mock.return_value.data = []
        pm = PositionManager()
        self.assertTrue(pm.is_initialized)
        self.assertEqual(pm.active_trades, [])

    @patch('position_manager.settings')
    async def test_lazy_initialization_failure_and_retry(self, mock_settings):
        mock_settings.supabase_url = "https://mock.supabase.co"
        mock_settings.supabase_key = "key"

        # Retry succeeds
        self.mock_client.execute_mock.side_effect = [Exception("Network Error"), MagicMock(data=[])]
        pm = PositionManager()
        self.assertFalse(pm.is_initialized)

        await pm.update_trades(24000.0)
        self.assertTrue(pm.is_initialized)

        # Retry fails (hits line 113)
        self.mock_client.execute_mock.side_effect = Exception("Network Error")
        pm_fail = PositionManager()
        self.assertFalse(pm_fail.is_initialized)
        await pm_fail.update_trades(24000.0)
        self.assertFalse(pm_fail.is_initialized)

    @patch('position_manager.settings')
    def test_initialize_db_loads_open_trades_from_any_date(self, mock_settings):
        today_str = datetime.now(timezone.utc).isoformat()
        yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        last_week_str = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        self.mock_client.execute_mock.return_value.data = [
            {"id": "trade-yesterday-open", "created_at": yesterday_str, "state": "OPEN"},
            {"id": "trade-today-open", "created_at": today_str, "state": "OPEN"},
            {"id": "trade-old-t1", "created_at": last_week_str, "state": "T1_HIT",
             "entry_price": 24000.0, "stop_loss": 24000.0},
            {"id": "trade-old-closed", "created_at": last_week_str, "state": "CLOSED"},
            {"id": "trade-old-stopped", "created_at": last_week_str, "state": "STOPPED_OUT"},
        ]
        pm = PositionManager()
        self.assertTrue(pm.is_initialized)
        # Multi-day carry: nothing is ever deleted at startup
        self.mock_client.delete_mock.assert_not_called()
        loaded_ids = {t["id"] for t in pm.active_trades}
        self.assertEqual(
            loaded_ids,
            {"trade-yesterday-open", "trade-today-open", "trade-old-t1"}
        )
        # T1_HIT trade resumes with its trailed stop intact
        t1_trade = next(t for t in pm.active_trades if t["id"] == "trade-old-t1")
        self.assertEqual(t1_trade["stop_loss"], t1_trade["entry_price"])

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_previous_day_trade_continues_to_exit(self, mock_settings, mock_send_trade_update):
        """A previous-day OPEN trade must keep being evaluated until T1/T2/SL."""
        mock_settings.time_stop_minutes = 45
        yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.mock_client.execute_mock.return_value.data = [{
            "id": "trade-carryover",
            "signal_id": "0001",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24035.0,
            "target_2": 24070.0,
            "state": "OPEN",
            "created_at": yesterday_str,
        }]
        mock_send_trade_update.return_value = None
        pm = PositionManager()
        self.assertEqual(len(pm.active_trades), 1)

        with patch.object(pm.analytics, 'log_exit'):
            await pm.update_trades(24071.0)  # gaps past T2 next morning

        self.assertEqual(pm.active_trades[0]["state"], "CLOSED")

    @patch('position_manager.settings')
    async def test_add_trade_success_and_exception_safety(self, mock_settings):
        mock_settings.trade_dedupe_tolerance_pts = 1.0
        pm = PositionManager()
        pm.analytics.log_entry = MagicMock()
        
        signal = AresSignal(
            setup_type=SetupType.OI_WALL_REJECTION,
            direction=Direction.BULLISH,
            trigger_price=24000.0,
            entry_zone=(23990.0, 24010.0),
            stop_loss=23975.0,
            target_1=24050.0,
            target_2=24100.0,
            confidence="HIGH",
            reasons=["Reason 1"],
            timestamp=datetime.now(),
            strike_to_trade=24000,
            option_type="CE"
        )
        
        # Test Success path
        pm.add_trade(signal, 24001.0, None)
        self.assertEqual(len(pm.active_trades), 1)
        await asyncio.sleep(0.05)
        self.mock_client.insert_mock.assert_called_once()

        # Test Exception safety (Supabase fails, AnalyticsLogger fails)
        # (entries spread >1pt apart so the TASK-172 duplicate guard stays out
        # of the way — dedupe has its own dedicated tests)
        self.mock_client.insert_mock.reset_mock()
        self.mock_client.insert_mock.side_effect = Exception("Supabase insert error")
        with patch.object(pm.analytics, 'log_entry', side_effect=Exception("Analytics logger error")):
            pm.add_trade(signal, 24010.0, None)
            await asyncio.sleep(0.05)
            self.mock_client.insert_mock.assert_called_once()

        # Test line 96-97 get_running_loop error pathway
        with patch('asyncio.get_running_loop', side_effect=RuntimeError("No event loop")):
            pm.add_trade(signal, 24020.0, None)

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_trailing_stop_bullish(self, mock_settings, mock_send_trade_update):
        pm = PositionManager()
        trade = {
            "id": "trade-bullish",
            "signal_id": "1111",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN"
        }
        pm.active_trades = [trade]
        
        # 1. Price reaches T1 -> state trails to T1_HIT, SL = entry
        # (TASK-172 fill-at-level: events are reported at the touched level)
        await pm.update_trades(24060.0)
        self.assertEqual(trade["state"], "T1_HIT")
        self.assertEqual(trade["stop_loss"], 24000.0)
        mock_send_trade_update.assert_called_with(trade, 24050.0, "T1_HIT")

        # 2. Price hits SL -> CLOSED, update_type = STOPPED_OUT_AT_BE
        mock_send_trade_update.reset_mock()
        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            await pm.update_trades(23999.0)
            self.assertEqual(trade["state"], "CLOSED")
            mock_send_trade_update.assert_called_with(trade, 24000.0, "STOPPED_OUT_AT_BE")
            mock_log_exit.assert_called_with("trade-bullish", 24000.0, "STOPPED_OUT_AT_BE")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_trailing_stop_bearish(self, mock_settings, mock_send_trade_update):
        pm = PositionManager()
        trade = {
            "id": "trade-bearish",
            "signal_id": "2222",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BEARISH",
            "entry_price": 24000.0,
            "stop_loss": 24025.0,
            "target_1": 23950.0,
            "target_2": 23900.0,
            "state": "OPEN"
        }
        closed_trade = {
            "id": "trade-closed",
            "state": "CLOSED"
        }
        pm.active_trades = [trade, closed_trade]
        
        # 1. Price reaches T1 -> state trails to T1_HIT, SL = entry
        # (TASK-172 fill-at-level: events are reported at the touched level)
        await pm.update_trades(23940.0)
        self.assertEqual(trade["state"], "T1_HIT")
        self.assertEqual(trade["stop_loss"], 24000.0)
        mock_send_trade_update.assert_called_with(trade, 23950.0, "T1_HIT")

        # 2. Price hits trailed SL -> CLOSED, update_type = STOPPED_OUT_AT_BE
        mock_send_trade_update.reset_mock()
        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            await pm.update_trades(24001.0)
            self.assertEqual(trade["state"], "CLOSED")
            mock_send_trade_update.assert_called_with(trade, 24000.0, "STOPPED_OUT_AT_BE")
            mock_log_exit.assert_called_with("trade-bearish", 24000.0, "STOPPED_OUT_AT_BE")

        # Reset for Bearish T2 Hit
        trade_t2 = {
            "id": "trade-bearish-t2",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BEARISH",
            "entry_price": 24000.0,
            "stop_loss": 24025.0,
            "target_1": 23950.0,
            "target_2": 23900.0,
            "state": "OPEN"
        }
        pm.active_trades = [trade_t2]
        mock_send_trade_update.reset_mock()
        await pm.update_trades(23890.0)
        self.assertEqual(trade_t2["state"], "CLOSED")
        mock_send_trade_update.assert_called_with(trade_t2, 23900.0, "T2_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_time_stop_tightens_open_trade_to_breakeven(self, mock_settings, mock_send_trade_update):
        """TASK-171: OPEN trade with no T1 progress after time_stop_minutes gets SL moved to entry."""
        mock_settings.time_stop_minutes = 45
        stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=46)).isoformat()
        trade = {
            "id": "trade-stale",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN",
            "created_at": stale_ts,
        }
        pm = PositionManager()
        pm.active_trades = [trade]

        # Price drifting, no SL/T1 touch: time-stop should tighten SL to entry
        events = await pm.update_trades(24010.0)
        self.assertEqual(trade["stop_loss"], 24000.0)
        self.assertEqual(trade["state"], "OPEN")

        # Tightened breakeven hit -> exits as TIME_STOP, not a fake T1 win
        # (fill-at-level: exit reported at the tightened stop, i.e. entry)
        events = await pm.update_trades(23999.0)
        self.assertEqual(trade["state"], "CLOSED")
        self.assertIn(("trade-stale", "TIME_STOP"), events)
        mock_send_trade_update.assert_called_with(trade, 24000.0, "TIME_STOP")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_time_stop_ignores_fresh_trades_and_bearish_tighten(self, mock_settings, mock_send_trade_update):
        mock_settings.time_stop_minutes = 45
        fresh_ts = datetime.now(timezone.utc).isoformat()
        stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        fresh = {
            "id": "trade-fresh", "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH", "entry_price": 24000.0, "stop_loss": 23975.0,
            "target_1": 24050.0, "target_2": 24100.0, "state": "OPEN",
            "created_at": fresh_ts,
        }
        stale_bear = {
            "id": "trade-stale-bear", "setup_type": "EXHAUSTION_REVERSAL",
            "direction": "BEARISH", "entry_price": 24000.0, "stop_loss": 24025.0,
            "target_1": 23950.0, "target_2": 23900.0, "state": "OPEN",
            "created_at": stale_ts,
        }
        pm = PositionManager()
        pm.active_trades = [fresh, stale_bear]

        await pm.update_trades(24005.0)
        self.assertEqual(fresh["stop_loss"], 23975.0)      # untouched
        self.assertEqual(stale_bear["stop_loss"], 24000.0)  # tightened to entry

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_returns_sl_hit_events(self, mock_settings, mock_send_trade_update):
        """TASK-171: exit events are returned so main can clear the engine cooldown."""
        trade = {
            "id": "trade-sl-event", "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH", "entry_price": 24000.0, "stop_loss": 23975.0,
            "target_1": 24050.0, "target_2": 24100.0, "state": "OPEN",
        }
        pm = PositionManager()
        pm.active_trades = [trade]

        events = await pm.update_trades(23970.0)
        self.assertIn(("trade-sl-event", "SL_HIT"), events)

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_regular_sl_hit_bullish(self, mock_settings, mock_send_trade_update):
        pm = PositionManager()
        trade = {
            "id": "trade-sl",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN"
        }
        pm.active_trades = [trade]
        
        await pm.update_trades(23970.0)
        self.assertEqual(trade["state"], "CLOSED")
        mock_send_trade_update.assert_called_with(trade, 23975.0, "SL_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_regular_sl_hit_bearish(self, mock_settings, mock_send_trade_update):
        pm = PositionManager()
        trade = {
            "id": "trade-sl",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BEARISH",
            "entry_price": 24000.0,
            "stop_loss": 24025.0,
            "target_1": 23950.0,
            "target_2": 23900.0,
            "state": "OPEN"
        }
        pm.active_trades = [trade]
        
        await pm.update_trades(24030.0)
        self.assertEqual(trade["state"], "CLOSED")
        mock_send_trade_update.assert_called_with(trade, 24025.0, "SL_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_exception_safety(self, mock_settings, mock_send_trade_update):
        pm = PositionManager()
        trade = {
            "id": "trade-err",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN"
        }
        pm.active_trades = [trade]

        self.mock_client.update_mock.side_effect = Exception("Supabase select/update error")
        pm.analytics.log_exit = MagicMock(side_effect=Exception("DB logger down"))
        mock_send_trade_update.side_effect = Exception("Discord alert fail")

        await pm.update_trades(24105.0)
        await asyncio.sleep(0.05)
        self.assertEqual(trade["state"], "CLOSED")

        # Test line 179-180 get_running_loop error pathway in _update
        trade_loop_err = {
            "id": "trade-loop-err",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN"
        }
        pm.active_trades = [trade_loop_err]
        with patch('asyncio.get_running_loop', side_effect=RuntimeError("No event loop")):
            await pm.update_trades(24105.0)

class TestIntrabarExitsAndDedup(unittest.IsolatedAsyncioTestCase):
    """TASK-172 audit P1: intrabar high/low exit detection (item 11) and
    duplicate add_trade guard (item 13)."""

    def setUp(self):
        self.mock_client = global_mock_client
        self.mock_client.insert_mock.reset_mock()
        self.mock_client.update_mock.reset_mock()
        self.mock_client.insert_mock.side_effect = None
        self.mock_client.update_mock.side_effect = None
        self.mock_client.execute_mock.side_effect = None
        self.mock_client.execute_mock.return_value.data = []

    def _bullish_trade(self, **overrides):
        trade = {
            "id": "trade-intrabar",
            "signal_id": "0042",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN",
        }
        trade.update(overrides)
        return trade

    def _make_signal(self, setup_type=SetupType.OI_WALL_REJECTION):
        return AresSignal(
            setup_type=setup_type,
            direction=Direction.BULLISH,
            trigger_price=24000.0,
            entry_zone=(23990.0, 24010.0),
            stop_loss=23975.0,
            target_1=24050.0,
            target_2=24100.0,
            confidence="HIGH",
            reasons=["Reason 1"],
            timestamp=datetime.now(),
            strike_to_trade=24000,
            option_type="CE",
        )

    # ── Item 11: intrabar exit detection ────────────────────────────────────

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_intrabar_low_triggers_sl_even_if_close_recovers(self, mock_settings, mock_alert):
        """Candle wicks below SL but closes back above it: close-only polling
        missed this stop (avg +6.8 pts slippage); intrabar detection catches it
        and fills at the stop level."""
        trade = self._bullish_trade()
        pm = PositionManager()
        pm.active_trades = [trade]

        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            events = await pm.update_trades(23990.0, candle_high=23995.0, candle_low=23970.0)

        self.assertEqual(trade["state"], "CLOSED")
        self.assertIn(("trade-intrabar", "SL_HIT"), events)
        mock_alert.assert_called_with(trade, 23975.0, "SL_HIT")
        mock_log_exit.assert_called_with("trade-intrabar", 23975.0, "SL_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_intrabar_high_triggers_t1_even_if_close_below(self, mock_settings, mock_alert):
        trade = self._bullish_trade()
        pm = PositionManager()
        pm.active_trades = [trade]

        await pm.update_trades(24040.0, candle_high=24052.0, candle_low=24035.0)

        self.assertEqual(trade["state"], "T1_HIT")
        self.assertEqual(trade["stop_loss"], 24000.0)
        mock_alert.assert_called_with(trade, 24050.0, "T1_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_intrabar_both_sl_and_target_resolves_optimistically(self, mock_settings, mock_alert):
        """A candle that spans both the stop and a target is resolved as a
        target hit — an optimistic assumption so wicks that touch targets count as wins."""
        trade = self._bullish_trade()
        pm = PositionManager()
        pm.active_trades = [trade]

        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            # high goes to 24105 (past T2 = 24100), low goes to 23970 (past SL = 23975)
            events = await pm.update_trades(24080.0, candle_high=24105.0, candle_low=23970.0)

        self.assertEqual(trade["state"], "CLOSED")
        self.assertIn(("trade-intrabar", "T2_HIT"), events)
        mock_log_exit.assert_called_with("trade-intrabar", 24100.0, "T2_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_intrabar_t2_fills_at_level_not_close(self, mock_settings, mock_alert):
        trade = self._bullish_trade(state="T1_HIT", stop_loss=24000.0)
        pm = PositionManager()
        pm.active_trades = [trade]

        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            await pm.update_trades(24120.0, candle_high=24125.0, candle_low=24095.0)

        self.assertEqual(trade["state"], "CLOSED")
        # Exit recorded at T2 (24100), not the poll close (24120)
        mock_log_exit.assert_called_with("trade-intrabar", 24100.0, "T2_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_intrabar_bearish_high_triggers_sl(self, mock_settings, mock_alert):
        trade = self._bullish_trade(
            direction="BEARISH", stop_loss=24025.0,
            target_1=23950.0, target_2=23900.0,
        )
        pm = PositionManager()
        pm.active_trades = [trade]

        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            events = await pm.update_trades(24010.0, candle_high=24030.0, candle_low=24005.0)

        self.assertEqual(trade["state"], "CLOSED")
        self.assertIn(("trade-intrabar", "SL_HIT"), events)
        mock_log_exit.assert_called_with("trade-intrabar", 24025.0, "SL_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_close_only_call_still_works_without_candle(self, mock_settings, mock_alert):
        """Backward compatibility: omitting candle extremes falls back to the
        spot price for every check."""
        trade = self._bullish_trade()
        pm = PositionManager()
        pm.active_trades = [trade]

        await pm.update_trades(23990.0)  # above SL, below T1: nothing happens
        self.assertEqual(trade["state"], "OPEN")

        await pm.update_trades(23970.0)  # through SL
        self.assertEqual(trade["state"], "CLOSED")
        mock_alert.assert_called_with(trade, 23975.0, "SL_HIT")

    # ── Item 13: duplicate add_trade guard ──────────────────────────────────

    @patch('position_manager.settings')
    async def test_add_trade_skips_duplicate_signal(self, mock_settings):
        """The 06-29 14:12 OI wall trade was logged twice. A second add for the
        same setup/direction at (nearly) the same entry is now skipped."""
        mock_settings.trade_dedupe_tolerance_pts = 1.0
        pm = PositionManager()
        pm.analytics.log_entry = MagicMock()
        pm.active_trades = []
        signal = self._make_signal()

        pm.add_trade(signal, 24001.0, None)
        pm.add_trade(signal, 24001.4, None)  # same setup, entry within 1pt

        self.assertEqual(len(pm.active_trades), 1)
        await asyncio.sleep(0.05)
        self.mock_client.insert_mock.assert_called_once()

    @patch('position_manager.settings')
    async def test_add_trade_allows_same_setup_at_different_level(self, mock_settings):
        mock_settings.trade_dedupe_tolerance_pts = 1.0
        pm = PositionManager()
        pm.active_trades = []
        signal = self._make_signal()

        pm.add_trade(signal, 24001.0, None)
        pm.add_trade(signal, 24020.0, None)  # >1pt away: legitimate new trade

        self.assertEqual(len(pm.active_trades), 2)

    @patch('position_manager.settings')
    async def test_add_trade_allows_reentry_after_close(self, mock_settings):
        pm = PositionManager()
        pm.active_trades = []
        signal = self._make_signal()

        pm.add_trade(signal, 24001.0, None)
        pm.active_trades[0]["state"] = "CLOSED"
        pm.add_trade(signal, 24001.0, None)  # prior trade closed: re-entry OK

        self.assertEqual(len(pm.active_trades), 2)


def tearDownModule():
    mock_create_patch.stop()
    if 'position_manager' in sys.modules:
        importlib.reload(sys.modules['position_manager'])

if __name__ == '__main__':
    unittest.main()
