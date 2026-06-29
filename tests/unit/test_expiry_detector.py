import unittest
from unittest.mock import MagicMock, patch
import sys
from datetime import date
from detectors.expiry_detector import _today_ist, is_expiry_day_simple, is_expiry_day_from_api

class TestExpiryDetector(unittest.IsolatedAsyncioTestCase):

    def test_today_ist(self):
        d = _today_ist()
        self.assertIsInstance(d, date)

    @patch('detectors.expiry_detector._today_ist')
    def test_is_expiry_day_simple_tuesday(self, mock_today):
        # Tuesday (weekday = 1)
        mock_today.return_value = date(2026, 6, 30)
        self.assertTrue(is_expiry_day_simple())

    @patch('detectors.expiry_detector._today_ist')
    def test_is_expiry_day_simple_not_tuesday(self, mock_today):
        # Wednesday (weekday = 2)
        mock_today.return_value = date(2026, 7, 1)
        self.assertFalse(is_expiry_day_simple())

    @patch('detectors.expiry_detector._today_ist')
    @patch('dhanhq.dhanhq')
    @patch('config.settings')
    async def test_api_success_today_in_list(self, mock_settings, mock_dhanhq, mock_today):
        mock_today.return_value = date(2026, 6, 30)
        mock_settings.dhan_client_id = "id"
        mock_settings.dhan_access_token = "token"
        mock_settings.security_id = "123"
        mock_settings.exchange_segment = "segment"

        mock_dhan = MagicMock()
        mock_dhan.expiry_list.return_value = {
            "status": "success",
            "data": {
                "data": ["2026-06-29", "2026-06-30", "2026-07-07"]
            }
        }
        mock_dhanhq.return_value = mock_dhan

        result = await is_expiry_day_from_api()
        self.assertTrue(result)

    @patch('detectors.expiry_detector._today_ist')
    @patch('dhanhq.dhanhq')
    @patch('config.settings')
    async def test_api_success_nearest_is_today(self, mock_settings, mock_dhanhq, mock_today):
        # Use a non-standard date format like "2026-6-30" to bypass exact string check (line 62)
        # but match in strptime (line 71)
        mock_today.return_value = date(2026, 6, 30)
        mock_settings.dhan_client_id = "id"
        mock_settings.dhan_access_token = "token"

        mock_dhan = MagicMock()
        mock_dhan.expiry_list.return_value = {
            "status": "success",
            "data": {
                "data": ["2026-6-30"]
            }
        }
        mock_dhanhq.return_value = mock_dhan

        result = await is_expiry_day_from_api()
        self.assertTrue(result)

    @patch('detectors.expiry_detector._today_ist')
    @patch('dhanhq.dhanhq')
    @patch('config.settings')
    async def test_api_success_nearest_is_not_today(self, mock_settings, mock_dhanhq, mock_today):
        mock_today.return_value = date(2026, 6, 30)
        mock_settings.dhan_client_id = "id"
        mock_settings.dhan_access_token = "token"

        mock_dhan = MagicMock()
        mock_dhan.expiry_list.return_value = {
            "status": "success",
            "data": {
                "data": ["2026-07-07"]
            }
        }
        mock_dhanhq.return_value = mock_dhan

        result = await is_expiry_day_from_api()
        self.assertFalse(result)

    @patch('detectors.expiry_detector._today_ist')
    @patch('dhanhq.dhanhq')
    @patch('config.settings')
    async def test_api_success_invalid_date_format(self, mock_settings, mock_dhanhq, mock_today):
        mock_today.return_value = date(2026, 6, 30)
        mock_settings.dhan_client_id = "id"
        mock_settings.dhan_access_token = "token"

        mock_dhan = MagicMock()
        mock_dhan.expiry_list.return_value = {
            "status": "success",
            "data": {
                "data": ["invalid-date"]
            }
        }
        mock_dhanhq.return_value = mock_dhan

        result = await is_expiry_day_from_api()
        self.assertFalse(result)

    @patch('detectors.expiry_detector._today_ist')
    @patch('dhanhq.dhanhq')
    @patch('config.settings')
    async def test_api_failure_fallback_to_tuesday(self, mock_settings, mock_dhanhq, mock_today):
        mock_today.return_value = date(2026, 6, 30)
        mock_settings.dhan_client_id = "id"
        mock_settings.dhan_access_token = "token"

        mock_dhan = MagicMock()
        mock_dhan.expiry_list.return_value = {"status": "failure"}
        mock_dhanhq.return_value = mock_dhan

        result = await is_expiry_day_from_api()
        self.assertTrue(result)

    @patch('detectors.expiry_detector._today_ist')
    @patch('dhanhq.dhanhq')
    @patch('config.settings')
    async def test_api_exception_fallback_to_tuesday(self, mock_settings, mock_dhanhq, mock_today):
        mock_today.return_value = date(2026, 7, 1)
        mock_settings.dhan_client_id = "id"
        mock_settings.dhan_access_token = "token"

        mock_dhanhq.side_effect = Exception("API connection down")

        result = await is_expiry_day_from_api()
        self.assertFalse(result)

    @patch('detectors.expiry_detector._today_ist')
    @patch('config.settings')
    async def test_dhanhq_import_error_fallback(self, mock_settings, mock_today):
        mock_today.return_value = date(2026, 6, 30)
        mock_settings.dhan_client_id = "id"
        mock_settings.dhan_access_token = "token"
        
        # Mock sys.modules for dhanhq so DhanContext raises AttributeError (which acts as ImportError)
        mock_dhanhq_module = MagicMock()
        if hasattr(mock_dhanhq_module, 'DhanContext'):
            del mock_dhanhq_module.DhanContext
            
        mock_dhan = MagicMock()
        mock_dhan.expiry_list.return_value = {
            "status": "success",
            "data": {
                "data": ["2026-06-30"]
            }
        }
        # dhanhq() constructor call should return mock_dhan
        mock_dhanhq_module.dhanhq.return_value = mock_dhan
        
        # Save original sys.modules
        orig_dhanhq = sys.modules.get('dhanhq')
        sys.modules['dhanhq'] = mock_dhanhq_module
        try:
            result = await is_expiry_day_from_api()
            self.assertTrue(result)
        finally:
            if orig_dhanhq:
                sys.modules['dhanhq'] = orig_dhanhq
            else:
                del sys.modules['dhanhq']

if __name__ == '__main__':
    unittest.main()
