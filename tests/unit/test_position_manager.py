import asyncio
import unittest
from unittest.mock import patch, MagicMock
from position_manager import PositionManager

class TestPositionManager(unittest.TestCase):

    @patch('position_manager.create_client')
    @patch('position_manager.AnalyticsLogger')
    def test_lazy_initialization_success(self, mock_analytics, mock_create_client):
        # Setup mock for successful DB fetch
        mock_supabase = MagicMock()
        mock_response = MagicMock()
        mock_response.data = []
        mock_supabase.table().select().execute.return_value = mock_response
        mock_create_client.return_value = mock_supabase

        pm = PositionManager()
        
        self.assertTrue(pm.is_initialized)
        self.assertEqual(pm.active_trades, [])

    @patch('position_manager.create_client')
    @patch('position_manager.AnalyticsLogger')
    def test_lazy_initialization_failure_and_retry(self, mock_analytics, mock_create_client):
        # Setup mock for failing DB fetch initially
        mock_supabase = MagicMock()
        mock_supabase.table().select().execute.side_effect = [Exception("Network Error"), MagicMock(data=[])]
        mock_create_client.return_value = mock_supabase

        pm = PositionManager()
        
        # Initially, initialization should fail
        self.assertFalse(pm.is_initialized)
        self.assertEqual(pm.active_trades, [])
        
        # When update_trades is called, it should try again and succeed
        asyncio.run(pm.update_trades(24000.0))
        
        self.assertTrue(pm.is_initialized)
        self.assertEqual(mock_supabase.table().select().execute.call_count, 2)

if __name__ == '__main__':
    unittest.main()
