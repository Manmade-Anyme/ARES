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
    def test_initialize_db_with_old_and_valid_records(self, mock_settings):
        today_str = datetime.now(timezone.utc).isoformat()
        yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        
        self.mock_client.execute_mock.return_value.data = [
            {"id": "trade-old", "created_at": yesterday_str, "state": "OPEN"},
            {"id": "trade-valid", "created_at": today_str, "state": "OPEN"},
            {"id": "trade-invalid-date", "created_at": "invalid-date", "state": "OPEN"}
        ]
        pm = PositionManager()
        self.assertTrue(pm.is_initialized)
        self.mock_client.delete_mock.assert_called_once()
        self.assertEqual(len(pm.active_trades), 1)
        self.assertEqual(pm.active_trades[0]["id"], "trade-valid")

    @patch('position_manager.settings')
    async def test_add_trade_success_and_exception_safety(self, mock_settings):
        pm = PositionManager()
        
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
        self.mock_client.insert_mock.reset_mock()
        self.mock_client.insert_mock.side_effect = Exception("Supabase insert error")
        with patch.object(pm.analytics, 'log_entry', side_effect=Exception("Analytics logger error")):
            pm.add_trade(signal, 24002.0, None)
            await asyncio.sleep(0.05)
            self.mock_client.insert_mock.assert_called_once()

        # Test line 96-97 get_running_loop error pathway
        with patch('asyncio.get_running_loop', side_effect=RuntimeError("No event loop")):
            pm.add_trade(signal, 24003.0, None)

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
        await pm.update_trades(24060.0)
        self.assertEqual(trade["state"], "T1_HIT")
        self.assertEqual(trade["stop_loss"], 24000.0)
        mock_send_trade_update.assert_called_with(trade, 24060.0, "T1_HIT")

        # 2. Price hits SL -> CLOSED, update_type = T1_HIT
        mock_send_trade_update.reset_mock()
        await pm.update_trades(23999.0)
        self.assertEqual(trade["state"], "CLOSED")
        mock_send_trade_update.assert_called_with(trade, 23999.0, "T1_HIT")

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
        await pm.update_trades(23940.0)
        self.assertEqual(trade["state"], "T1_HIT")
        self.assertEqual(trade["stop_loss"], 24000.0)
        mock_send_trade_update.assert_called_with(trade, 23940.0, "T1_HIT")

        # 2. Price hits trailed SL -> CLOSED, update_type = T1_HIT
        mock_send_trade_update.reset_mock()
        await pm.update_trades(24001.0)
        self.assertEqual(trade["state"], "CLOSED")
        mock_send_trade_update.assert_called_with(trade, 24001.0, "T1_HIT")

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
        mock_send_trade_update.assert_called_with(trade_t2, 23890.0, "T2_HIT")

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
        mock_send_trade_update.assert_called_with(trade, 23970.0, "SL_HIT")

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
        mock_send_trade_update.assert_called_with(trade, 24030.0, "SL_HIT")

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

def tearDownModule():
    mock_create_patch.stop()
    if 'position_manager' in sys.modules:
        importlib.reload(sys.modules['position_manager'])

if __name__ == '__main__':
    unittest.main()
