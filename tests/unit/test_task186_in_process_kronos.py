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
