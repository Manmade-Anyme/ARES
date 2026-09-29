import unittest
from unittest.mock import patch, MagicMock

from position_manager import PositionManager
from storage import Storage, AnalyticsLogger
from models import AresSignal, SetupType, Direction

class TestManm150Coverage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.signal = AresSignal(
            timestamp="2026-09-14T01:00:00Z",
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=Direction.BULLISH,
            trigger_price=24000.0,
            entry_zone=(23990.0, 24010.0),
            stop_loss=23950.0,
            target_1=24050.0,
            target_2=24100.0,
            confidence="HIGH",
            strike_to_trade=24000,
            option_type="CE",
            reasons=["Test reason"]
        )
        self.signal.suggested_lots = 5
        self.signal.capital = 100000.0
        self.signal.risk_pct = 2.0
        self.signal.option_sl = 100.0
        self.signal.option_target = 200.0
        self.signal.option_premium = 150.0
        self.signal.option_delta = 0.5
        self.signal.oi_wall_context = "test oi wall"
        self.signal.db_id = 999
        self.spot = 24000.0
        
        self.atm = MagicMock()
        self.atm.ce.oi = 100
        self.atm.pe.oi = 200
        self.atm.ce.oi_change_pct = 5.0
        self.atm.pe.oi_change_pct = 10.0

    @patch('position_manager.create_client')
    @patch('position_manager.settings')
    async def test_position_manager_unsupported_mode(self, mock_settings, mock_create_client):
        mock_settings.signal_schema_mode = "invalid_mode"
        pm = PositionManager()
        with self.assertRaisesRegex(ValueError, "Unsupported signal schema mode"):
            await pm.add_trade(self.signal, self.spot, self.atm)

    @patch('position_manager.create_client')
    @patch('position_manager.settings')
    async def test_position_manager_unexpected_trade_id(self, mock_settings, mock_create_client):
        mock_settings.signal_schema_mode = "greenfield"
        pm = PositionManager()
        pm.supabase.rpc.return_value.execute.return_value.data = ["wrong_uuid"]
        
        with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
            await pm.add_trade(self.signal, self.spot, self.atm)

    @patch('position_manager.create_client')
    @patch('position_manager.settings')
    async def test_position_manager_retry_exhaustion(self, mock_settings, mock_create_client):
        mock_settings.signal_schema_mode = "greenfield"
        pm = PositionManager()
        pm.supabase.rpc.return_value.execute.side_effect = Exception("DB error")
        
        with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
            await pm.add_trade(self.signal, self.spot, self.atm)

    @patch('storage.create_client')
    @patch('storage.settings')
    async def test_storage_unsupported_mode(self, mock_settings, mock_create_client):
        mock_settings.signal_schema_mode = "invalid_mode"
        storage = Storage()
        with self.assertRaisesRegex(RuntimeError, "Unsupported signal schema mode"):
            await storage.log_signal(self.signal, self.spot)
            
    @patch('storage.create_client')
    @patch('storage.settings')
    async def test_storage_no_persisted_row(self, mock_settings, mock_create_client):
        mock_settings.signal_schema_mode = "greenfield"
        storage = Storage()
        storage.supabase.table.return_value.insert.return_value.execute.return_value.data = []
        with self.assertRaisesRegex(RuntimeError, "Signal insert returned no persisted row"):
            await storage.log_signal(self.signal, self.spot)
            
    @patch('storage.create_client')
    @patch('storage.settings')
    async def test_storage_bridge_mismatched_uuid(self, mock_settings, mock_create_client):
        mock_settings.signal_schema_mode = "bridge"
        storage = Storage()
        storage.supabase.table.return_value.insert.return_value.execute.return_value.data = [{"signal_uuid": "wrong"}]
        with self.assertRaisesRegex(RuntimeError, "mismatched canonical UUID"):
            await storage.log_signal(self.signal, self.spot)
            
    @patch('storage.create_client')
    @patch('storage.settings')
    async def test_storage_bridge_no_legacy_id(self, mock_settings, mock_create_client):
        mock_settings.signal_schema_mode = "bridge"
        storage = Storage()
        storage.supabase.table.return_value.insert.return_value.execute.return_value.data = [{"signal_uuid": str(self.signal.id)}]
        with self.assertRaisesRegex(RuntimeError, "returned no legacy row id"):
            await storage.log_signal(self.signal, self.spot)
            
    @patch('storage.create_client')
    @patch('storage.settings')
    async def test_storage_greenfield_mismatched_uuid(self, mock_settings, mock_create_client):
        mock_settings.signal_schema_mode = "greenfield"
        storage = Storage()
        storage.supabase.table.return_value.insert.return_value.execute.return_value.data = [{"id": "wrong"}]
        with self.assertRaisesRegex(RuntimeError, "mismatched canonical UUID"):
            await storage.log_signal(self.signal, self.spot)
            
    @patch('builtins.print')
    @patch('storage.create_client')
    @patch('storage.settings')
    def test_analytics_log_exit_legacy_zero_rows(self, mock_settings, mock_create_client, mock_print):
        mock_settings.signal_schema_mode = "legacy"
        logger = AnalyticsLogger()
        def table_mock(name):
            mock = MagicMock()
            if name == "trade_analytics":
                mock.select.return_value.eq.return_value.execute.return_value.data = [{"entry_price": 24000.0, "direction": "BULLISH", "signal_id": "sig1", "signal_uuid": None}]
                mock.update.return_value.eq.return_value.execute.return_value.data = [{"id": "trade1"}]
            elif name == "ml_collection":
                mock.update.return_value.eq.return_value.execute.return_value.data = []
            return mock
        logger.supabase.table.side_effect = table_mock
        
        logger.log_exit("trade1", 24050.0, "T1_HIT")
        mock_print.assert_any_call("Storage: zero rows updated in ml_collection for signal_id sig1")

    @patch('builtins.print')
    @patch('storage.create_client')
    @patch('storage.settings')
    def test_analytics_log_exit_legacy_exception(self, mock_settings, mock_create_client, mock_print):
        mock_settings.signal_schema_mode = "legacy"
        logger = AnalyticsLogger()
        def table_mock(name):
            mock = MagicMock()
            if name == "trade_analytics":
                mock.select.return_value.eq.return_value.execute.return_value.data = [{"entry_price": 24000.0, "direction": "BULLISH", "signal_id": "sig1", "signal_uuid": None}]
                mock.update.return_value.eq.return_value.execute.return_value.data = [{"id": "trade1"}]
            elif name == "ml_collection":
                mock.update.return_value.eq.return_value.execute.side_effect = Exception("test err")
            return mock
        logger.supabase.table.side_effect = table_mock
        
        logger.log_exit("trade1", 24050.0, "T1_HIT")
        mock_print.assert_any_call("Failed to back-fill ml_collection label: test err")
        
    @patch('builtins.print')
    @patch('time.sleep')
    @patch('storage.create_client')
    @patch('storage.settings')
    def test_analytics_log_exit_bridge_retry_exhaustion_zero_rows(self, mock_settings, mock_create_client, mock_sleep, mock_print):
        mock_settings.signal_schema_mode = "bridge"
        logger = AnalyticsLogger()
        def table_mock(name):
            mock = MagicMock()
            if name == "trade_analytics":
                mock.select.return_value.eq.return_value.execute.return_value.data = [{"entry_price": 24000.0, "direction": "BULLISH", "signal_id": "sig1", "signal_uuid": "uuid1"}]
                mock.update.return_value.eq.return_value.execute.return_value.data = [{"id": "trade1"}]
            elif name == "ml_collection":
                mock.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
            return mock
        logger.supabase.table.side_effect = table_mock
        
        logger.log_exit("trade1", 24050.0, "T1_HIT")
        mock_print.assert_any_call("Storage: Terminal reconciliation error - zero rows updated in ml_collection for trade trade1 after 3 attempts")

    @patch('builtins.print')
    @patch('time.sleep')
    @patch('storage.create_client')
    @patch('storage.settings')
    def test_analytics_log_exit_bridge_retry_exception(self, mock_settings, mock_create_client, mock_sleep, mock_print):
        mock_settings.signal_schema_mode = "bridge"
        logger = AnalyticsLogger()
        def table_mock(name):
            mock = MagicMock()
            if name == "trade_analytics":
                mock.select.return_value.eq.return_value.execute.return_value.data = [{"entry_price": 24000.0, "direction": "BULLISH", "signal_id": "sig1", "signal_uuid": "uuid1"}]
                mock.update.return_value.eq.return_value.execute.return_value.data = [{"id": "trade1"}]
            elif name == "ml_collection":
                mock.update.return_value.eq.return_value.eq.return_value.execute.side_effect = Exception("test err")
            return mock
        logger.supabase.table.side_effect = table_mock
        
        logger.log_exit("trade1", 24050.0, "T1_HIT")
        mock_print.assert_any_call("Failed to back-fill ml_collection label: test err")


    @patch('builtins.print')
    @patch('storage.create_client')
    @patch('storage.settings')
    def test_analytics_log_exit_sl_hit(self, mock_settings, mock_create_client, mock_print):
        mock_settings.signal_schema_mode = "legacy"
        logger = AnalyticsLogger()
        def table_mock(name):
            mock = MagicMock()
            if name == "trade_analytics":
                mock.select.return_value.eq.return_value.execute.return_value.data = [{"entry_price": 24000.0, "direction": "BULLISH", "signal_id": "sig1", "signal_uuid": None}]
                mock.update.return_value.eq.return_value.execute.return_value.data = [{"id": "trade1"}]
            elif name == "ml_collection":
                mock.update.return_value.eq.return_value.execute.return_value.data = []
            return mock
        logger.supabase.table.side_effect = table_mock
        
        logger.log_exit("trade1", 23900.0, "SL_HIT")
        mock_print.assert_any_call("Storage: zero rows updated in ml_collection for signal_id sig1")

    @patch('builtins.print')
    @patch('storage.create_client')
    @patch('storage.settings')
    def test_analytics_log_exit_bridge_success(self, mock_settings, mock_create_client, mock_print):
        mock_settings.signal_schema_mode = "bridge"
        logger = AnalyticsLogger()
        def table_mock(name):
            mock = MagicMock()
            if name == "trade_analytics":
                mock.select.return_value.eq.return_value.execute.return_value.data = [{"entry_price": 24000.0, "direction": "BULLISH", "signal_id": "sig1", "signal_uuid": "uuid1"}]
                mock.update.return_value.eq.return_value.execute.return_value.data = [{"id": "trade1"}]
            elif name == "ml_collection":
                mock.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{"id": "ml_row1"}]
            return mock
        logger.supabase.table.side_effect = table_mock
        
        logger.log_exit("trade1", 24050.0, "T1_HIT")

    @patch('builtins.print')
    @patch('storage.create_client')
    @patch('storage.settings')
    def test_analytics_log_exit_outer_exception(self, mock_settings, mock_create_client, mock_print):
        logger = AnalyticsLogger()
        logger.supabase.table.side_effect = Exception("Outer exception test")
        logger.log_exit("trade1", 24050.0, "T1_HIT")
        mock_print.assert_any_call("Failed to log trade analytics exit: Outer exception test")


    @patch('builtins.print')
    @patch('storage.create_client')
    @patch('storage.settings')
    def test_analytics_log_exit_greenfield_success(self, mock_settings, mock_create_client, mock_print):
        mock_settings.signal_schema_mode = "greenfield"
        logger = AnalyticsLogger()
        def table_mock(name):
            from unittest.mock import MagicMock
            mock = MagicMock()
            if name == "trade_analytics":
                mock.select.return_value.eq.return_value.execute.return_value.data = [{"entry_price": 24000.0, "direction": "BULLISH", "signal_id": "sig1", "signal_uuid": None}]
                mock.update.return_value.eq.return_value.execute.return_value.data = [{"id": "trade1"}]
            elif name == "ml_collection":
                mock.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{"id": "ml_row1"}]
            return mock
        logger.supabase.table.side_effect = table_mock
        
        logger.log_exit("trade1", 24050.0, "T1_HIT")

if __name__ == '__main__':


    unittest.main()
