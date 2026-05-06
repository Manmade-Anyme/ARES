import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timezone
import uuid
import asyncio

# Mocking Supabase and settings before imports
with patch('supabase.create_client'), patch('config.settings'):
    from position_manager import PositionManager
    from models import AresSignal, SetupType, Direction, ATMStrikes, OptionRow
    from storage import AnalyticsLogger

class TestAnalyticsIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Mock Supabase client
        self.mock_supabase = MagicMock()
        
        # Patch create_client to return our mock
        self.patcher = patch('position_manager.create_client', return_value=self.mock_supabase)
        self.patcher.start()
        
        # Patch AnalyticsLogger's supabase client too
        self.analytics_patcher = patch('storage.create_client', return_value=self.mock_supabase)
        self.analytics_patcher.start()

        # Initialize PositionManager (this will call _initialize_db)
        # Mock _initialize_db to avoid real calls
        with patch.object(PositionManager, '_initialize_db'):
            self.pm = PositionManager()
            self.pm.supabase = self.mock_supabase
            self.pm.analytics.supabase = self.mock_supabase

    def tearDown(self):
        self.patcher.stop()
        self.analytics_patcher.stop()

    def test_add_trade_persistence(self):
        """Verify that add_trade updates both active_trades and trade_analytics."""
        signal = AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=Direction.BULLISH,
            trigger_price=24000.0,
            entry_zone=(24000.0, 24010.0),
            stop_loss=23950.0,
            target_1=24100.0,
            target_2=24200.0,
            confidence="HIGH",
            reasons=["Reason 1"],
            timestamp=datetime.now(timezone.utc),
            strike_to_trade=24000,
            option_type="CE"
        )
        spot = 24005.0
        
        # Mock atm data
        atm = MagicMock(spec=ATMStrikes)
        atm.ce = MagicMock(spec=OptionRow)
        atm.ce.oi = 1000000
        atm.ce.oi_change_pct = 5.0
        atm.pe = MagicMock(spec=OptionRow)
        atm.pe.oi = 1200000
        atm.pe.oi_change_pct = 10.0
        
        # Mock the async executor
        with patch('asyncio.get_running_loop') as mock_loop:
            self.pm.add_trade(signal, spot, atm=atm)
            
            # Verify memory state
            self.assertEqual(len(self.pm.active_trades), 1)
            trade = self.pm.active_trades[0]
            self.assertEqual(trade['state'], 'OPEN')
            self.assertEqual(trade['entry_price'], 24005.0)

            # Check if run_in_executor was called (for Supabase inserts)
            self.assertTrue(mock_loop.return_value.run_in_executor.called)
            
    @patch('position_manager.send_trade_update', new_callable=AsyncMock)
    async def test_update_trades_exit_persistence(self, mock_alert):
        """Verify that trade exit updates both active_trades and trade_analytics."""
        # Setup an active trade
        trade_id = str(uuid.uuid4())
        trade = {
            "id": trade_id,
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23950.0,
            "target_1": 24100.0,
            "target_2": 24200.0,
            "state": "OPEN"
        }
        self.pm.active_trades = [trade]
        
        # Mock loop and Supabase select for log_exit
        with patch('asyncio.get_running_loop') as mock_loop:
            # Mock the select response for log_exit
            mock_response = MagicMock()
            mock_response.data = [{"entry_price": 24000.0, "direction": "BULLISH"}]
            self.mock_supabase.table().select().eq().execute.return_value = mock_response
            
            # Trigger SL Hit
            await self.pm.update_trades(23900.0)
            
            # Verify memory state changed
            self.assertEqual(trade['state'], 'CLOSED')
            
            # Verify Supabase update was scheduled via run_in_executor
            # One for active_trades, one for trade_analytics update
            self.assertGreaterEqual(mock_loop.return_value.run_in_executor.call_count, 2)
            
            # Verify Discord alert was called
            self.assertTrue(mock_alert.called)

if __name__ == '__main__':
    unittest.main()
