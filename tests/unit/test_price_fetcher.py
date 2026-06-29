import unittest
from unittest.mock import MagicMock, patch
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from fetchers.price_fetcher import PriceFetcher
from models import OHLCVCandle

class TestPriceFetcher(unittest.IsolatedAsyncioTestCase):

    @patch('fetchers.price_fetcher.dhanhq')
    @patch('fetchers.price_fetcher.settings')
    def setUp(self, mock_settings, mock_dhanhq_class):
        mock_settings.dhan_client_id = "client_id"
        mock_settings.dhan_access_token = "access_token"
        mock_settings.security_id = "123"
        mock_settings.exchange_segment = "NSE"
        mock_settings.instrument_type = "INDEX"
        
        self.mock_dhan = MagicMock()
        mock_dhanhq_class.return_value = self.mock_dhan
        
        self.fetcher = PriceFetcher()

    def test_reset_vwap(self):
        self.fetcher.cumulative_tp_vol = 100.0
        self.fetcher.cumulative_vol = 50
        self.fetcher.reset_vwap()
        self.assertEqual(self.fetcher.cumulative_tp_vol, 0.0)
        self.assertEqual(self.fetcher.cumulative_vol, 0)

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_latest_candle_success_epoch_millis(self, mock_settings):
        now_ts_ms = int(datetime.now().timestamp() * 1000)
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "success",
            "data": {
                "start_Time": [now_ts_ms],
                "open": [24000.0],
                "high": [24050.0],
                "low": [23950.0],
                "close": [24010.0],
                "volume": [100]
            }
        }
        
        candle = await self.fetcher.fetch_latest_candle()
        self.assertIsInstance(candle, OHLCVCandle)
        self.assertEqual(candle.open, 24000.0)
        self.assertEqual(candle.close, 24010.0)
        self.assertEqual(candle.volume, 100)
        self.assertAlmostEqual(candle.vwap, 24003.3333, places=3)

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_latest_candle_success_string_timestamp(self, mock_settings):
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "success",
            "data": {
                "timestamp": ["2026-06-29 15:30:00"],
                "open": [24000.0],
                "high": [24050.0],
                "low": [23950.0],
                "close": [24010.0],
                "volume": [100]
            }
        }
        candle = await self.fetcher.fetch_latest_candle()
        self.assertEqual(candle.timestamp, datetime(2026, 6, 29, 15, 30, 0))

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_latest_candle_fallback_dateutil_parser(self, mock_settings):
        # Fallback date format (ISO style) for dateutil parser (line 167-169)
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "success",
            "data": {
                "timestamp": ["2026-06-29T15:30:00+05:30"],
                "open": [24000.0], "high": [24000.0], "low": [24000.0], "close": [24000.0], "volume": [10]
            }
        }
        candle = await self.fetcher.fetch_latest_candle()
        self.assertEqual(candle.timestamp.hour, 15)

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_latest_candle_timestamp_error(self, mock_settings):
        # Invalid timestamp string format that causes error (line 170-171)
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "success",
            "data": {
                "timestamp": ["completely-invalid-date"],
                "open": [24000.0], "high": [24000.0], "low": [24000.0], "close": [24000.0], "volume": [10]
            }
        }
        with self.assertRaises(ValueError):
            await self.fetcher.fetch_latest_candle()

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_latest_candle_zero_volume_vwap(self, mock_settings):
        # Volume is 0 -> vwap = typical_price (line 188)
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "success",
            "data": {
                "timestamp": ["2026-06-29 15:30:00"],
                "open": [24000.0], "high": [24000.0], "low": [24000.0], "close": [24000.0], "volume": [0]
            }
        }
        self.fetcher.cumulative_vol = 0
        candle = await self.fetcher.fetch_latest_candle()
        self.assertEqual(candle.vwap, 24000.0)

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_latest_candle_retries_and_failures(self, mock_settings):
        # 1. Transient API failure retry success
        self.mock_dhan.intraday_minute_data.side_effect = [
            {"status": "failure", "remarks": {"error_message": None}},
            {
                "status": "success",
                "data": {
                    "start_Time": [1719600000],
                    "open": [24000.0], "high": [24000.0], "low": [24000.0], "close": [24000.0], "volume": [10]
                }
            }
        ]
        candle = await self.fetcher.fetch_latest_candle()
        self.assertEqual(candle.open, 24000.0)

        # 2. Complete failure raising exception
        self.mock_dhan.intraday_minute_data.side_effect = Exception("Dhan is down")
        with self.assertRaises(Exception):
            await self.fetcher.fetch_latest_candle()

        # 3. Empty data response raising ValueError
        self.mock_dhan.intraday_minute_data.side_effect = None
        self.mock_dhan.intraday_minute_data.return_value = {"status": "success"}
        with self.assertRaises(ValueError):
            await self.fetcher.fetch_latest_candle()

        # 4. Exception raised on first attempt, then success but NO data (covers line 107)
        self.mock_dhan.intraday_minute_data.side_effect = [
            Exception("First attempt down"),
            {"status": "success"}
        ]
        with self.assertRaises(Exception) as ctx:
            await self.fetcher.fetch_latest_candle()
        self.assertIn("First attempt down", str(ctx.exception))

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_latest_candle_api_errors(self, mock_settings):
        # remarks dict error
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "failure",
            "remarks": {"error_message": "Invalid token authentication"},
            "data": {"errorType": "AuthError"}
        }
        with self.assertRaises(ValueError) as ctx:
            await self.fetcher.fetch_latest_candle()
        self.assertIn("Invalid token authentication", str(ctx.exception))

        # remarks string error (line 121)
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "failure",
            "remarks": "API Limit Exceeded",
            "data": {}
        }
        with self.assertRaises(ValueError) as ctx:
            await self.fetcher.fetch_latest_candle()
        self.assertIn("API Limit Exceeded", str(ctx.exception))

        # data errorMessage fallback (line 126)
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "failure",
            "remarks": None,
            "data": {"errorMessage": "Resource not found"}
        }
        with self.assertRaises(ValueError) as ctx:
            await self.fetcher.fetch_latest_candle()
        self.assertIn("Resource not found", str(ctx.exception))

        # data string fallback (line 128-129)
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "failure",
            "remarks": None,
            "data": "Raw server error string"
        }
        with self.assertRaises(ValueError) as ctx:
            await self.fetcher.fetch_latest_candle()
        self.assertIn("Raw server error string", str(ctx.exception))

        # Unknown empty remarks error (line 132)
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "failure",
            "remarks": None,
            "data": None
        }
        with self.assertRaises(ValueError) as ctx:
            await self.fetcher.fetch_latest_candle()
        self.assertIn("Unknown API error (Empty Remarks)", str(ctx.exception))

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_latest_candle_data_type_errors(self, mock_settings):
        # data is not a dict (line 138)
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "success",
            "data": []
        }
        with self.assertRaises(ValueError):
            await self.fetcher.fetch_latest_candle()

        # empty time array (line 150)
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "success",
            "data": {
                "timestamp": [],
                "open": [], "high": [], "low": [], "close": [], "volume": []
            }
        }
        with self.assertRaises(ValueError):
            await self.fetcher.fetch_latest_candle()

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_latest_candle_malformed_keys(self, mock_settings):
        self.mock_dhan.intraday_minute_data.return_value = {
            "status": "success",
            "data": {
                "start_Time": [1719600000],
                "open": [24000.0]
            }
        }
        with self.assertRaises(ValueError):
            await self.fetcher.fetch_latest_candle()

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_previous_day_ohlc_success_yesterday(self, mock_settings):
        yesterday_ts = int((datetime.now() - timedelta(days=1)).timestamp())
        self.mock_dhan.historical_daily_data.return_value = {
            "status": "success",
            "data": {
                "timestamp": [yesterday_ts],
                "high": [24102.35],
                "low": [23995.10]
            }
        }
        pdh, pdl = await self.fetcher.fetch_previous_day_ohlc()
        self.assertAlmostEqual(pdh, 24102.35, places=4)
        self.assertAlmostEqual(pdl, 23995.10, places=4)

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_previous_day_ohlc_success_today_fallback(self, mock_settings):
        today_ts = int(datetime.now().timestamp())
        yesterday_ts = int((datetime.now() - timedelta(days=1)).timestamp())
        self.mock_dhan.historical_daily_data.return_value = {
            "status": "success",
            "data": {
                "timestamp": [yesterday_ts, today_ts],
                "high": [24100.0, 24200.0],
                "low": [23900.0, 24150.0]
            }
        }
        pdh, pdl = await self.fetcher.fetch_previous_day_ohlc()
        self.assertAlmostEqual(pdh, 24100.0, places=4)
        self.assertAlmostEqual(pdl, 23900.0, places=4)

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_previous_day_ohlc_insufficient_data(self, mock_settings):
        today_ts = int(datetime.now().timestamp())
        self.mock_dhan.historical_daily_data.return_value = {
            "status": "success",
            "data": {
                "timestamp": [today_ts],
                "high": [24200.0],
                "low": [24150.0]
            }
        }
        with self.assertRaises(ValueError):
            await self.fetcher.fetch_previous_day_ohlc()

        # empty historical keys (line 249)
        self.mock_dhan.historical_daily_data.return_value = {
            "status": "success",
            "data": {
                "timestamp": [],
                "high": [],
                "low": []
            }
        }
        with self.assertRaises(ValueError):
            await self.fetcher.fetch_previous_day_ohlc()

    @patch('fetchers.price_fetcher.settings')
    async def test_fetch_previous_day_ohlc_failures_and_retries(self, mock_settings):
        # 1. Retries on exception -> retry success
        self.mock_dhan.historical_daily_data.side_effect = [
            Exception("Dhan temporary historical error"),
            {
                "status": "success",
                "data": {
                    "timestamp": [1719500000],
                    "high": [24000.0],
                    "low": [23900.0]
                }
            }
        ]
        pdh, pdl = await self.fetcher.fetch_previous_day_ohlc()
        self.assertAlmostEqual(pdh, 24000.0, places=4)

        # 2. Retries on failure status (status != success) -> success (covers lines 231-232)
        self.mock_dhan.historical_daily_data.side_effect = [
            {"status": "failure"},
            {
                "status": "success",
                "data": {
                    "timestamp": [1719500000],
                    "high": [24000.0],
                    "low": [23900.0]
                }
            }
        ]
        pdh, pdl = await self.fetcher.fetch_previous_day_ohlc()
        self.assertAlmostEqual(pdh, 24000.0, places=4)

        # 3. Permanent historical failure raising ValueError (covers lines 240-241)
        self.mock_dhan.historical_daily_data.side_effect = None
        self.mock_dhan.historical_daily_data.return_value = None
        with self.assertRaises(ValueError):
            await self.fetcher.fetch_previous_day_ohlc()

        # 4. Permanent exception raising
        self.mock_dhan.historical_daily_data.side_effect = Exception("Permanent failure")
        with self.assertRaises(Exception):
            await self.fetcher.fetch_previous_day_ohlc()

    @patch('dhanhq.DhanContext')
    @patch('fetchers.price_fetcher.settings')
    def test_import_dhan_context_fallback(self, mock_settings, mock_dhan_context):
        # Trigger ImportError inside constructor by setting instantiation side_effect to ImportError
        mock_dhan_context.side_effect = ImportError()
        with patch('fetchers.price_fetcher.dhanhq') as mock_dhanhq:
            PriceFetcher()
            mock_dhanhq.assert_called_once()

if __name__ == '__main__':
    unittest.main()
