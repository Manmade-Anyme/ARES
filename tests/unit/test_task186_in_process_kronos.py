import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
from datetime import datetime

from ml_signal.kronos_consumer import KronosConsumer, DEFAULT_CONFIG
from ml_signal.config import MLConfig


class TestTask186InProcessKronos(unittest.TestCase):

    @patch("ml_signal.kronos_consumer.load_dhan_credentials_from_supabase")
    @patch("ml_signal.kronos_consumer.create_client")
    def test_kronos_consumer_initialization(self, mock_create_client, mock_load_dhan):
        mock_load_dhan.return_value = ("client123", "token123")
        config = MLConfig(discord_webhook_url="https://discord.com/api/webhooks/mock")
        consumer = KronosConsumer(config)
        self.assertEqual(consumer.config.discord_webhook_url, "https://discord.com/api/webhooks/mock")
        self.assertEqual(len(consumer._processed_ids), 0)

    @patch("ml_signal.kronos_consumer.send_discord", new_callable=AsyncMock)
    @patch("ml_signal.kronos_consumer.load_dhan_credentials_from_supabase")
    @patch("ml_signal.kronos_consumer.create_client")
    def test_kronos_consumer_seeds_past_signals_on_startup(self, mock_create_client, mock_load_dhan, mock_send_discord):
        mock_load_dhan.return_value = ("client123", "token123")
        consumer = KronosConsumer(DEFAULT_CONFIG)
        
        # Mock Supabase table query returning 2 past signals
        mock_supabase = MagicMock()
        mock_query = MagicMock()
        mock_query.gte.return_value.order.return_value.execute.return_value.data = [
            {"id": "7767", "setup_type": "FAILED_BREAKOUT", "timestamp": "2026-07-13 10:00:00"},
            {"id": "7768", "setup_type": "TREND_CONTINUATION", "timestamp": "2026-07-13 11:25:00"},
        ]
        mock_supabase.table.return_value.select.return_value = mock_query
        consumer._supabase = mock_supabase

        # Run fetch_new_signals
        signals = asyncio.run(consumer.fetch_new_signals())
        self.assertEqual(len(signals), 2)
        self.assertEqual(signals[0]["id"], "7767")

    @patch("ml_signal.kronos_consumer.send_discord", new_callable=AsyncMock)
    @patch("ml_signal.kronos_consumer.load_dhan_credentials_from_supabase")
    @patch("ml_signal.kronos_consumer.create_client")
    def test_kronos_consumer_utc_timezone_recent_signal_detection(self, mock_create_client, mock_load_dhan, mock_send_discord):
        import pandas as pd
        mock_load_dhan.return_value = ("client123", "token123")
        consumer = KronosConsumer(DEFAULT_CONFIG)
        
        now_utc = pd.Timestamp.now(tz="UTC")
        recent_ts_str = (now_utc - pd.Timedelta(seconds=30)).isoformat()
        old_ts_str = (now_utc - pd.Timedelta(minutes=10)).isoformat()

        signals = [
            {"id": "old_1", "created_at": old_ts_str},
            {"id": "recent_1", "created_at": recent_ts_str},
        ]

        mock_supabase = MagicMock()
        mock_supabase.table.return_value.select.return_value.gte.return_value.order.return_value.execute.return_value.data = signals
        consumer._supabase = mock_supabase

        # Execute run loop logic for 1 iteration
        with patch.object(consumer, "_init_all"), \
             patch.object(consumer, "_forecast_probability", return_value=0.75), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(consumer.run("http://url", "key"))

        # old_1 should be seeded in _processed_ids, recent_1 should NOT be in _processed_ids before iteration
        # and mock_send_discord should be called for recent_1
        self.assertIn("old_1", consumer._processed_ids)
        self.assertIn("recent_1", consumer._processed_ids)
        mock_send_discord.assert_called_once()

    @patch("main.settings")
    def test_start_in_process_kronos_consumer_helper(self, mock_settings):
        mock_settings.discord_webhook_url = "https://discord.com/test"
        mock_settings.supabase_url = "https://mock.supabase.co"
        mock_settings.supabase_key = "mock_key"

        from main import _start_in_process_kronos_consumer

        with patch("ml_signal.kronos_consumer.KronosConsumer.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = None
            asyncio.run(_start_in_process_kronos_consumer())
            mock_run.assert_called_once_with(supabase_url="https://mock.supabase.co", supabase_key="mock_key")


if __name__ == "__main__":
    unittest.main()
