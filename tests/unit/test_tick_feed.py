"""
Tests for TASK-173 audit P2 item 18: TickFeed, a thin wrapper around dhanhq's
WebSocket MarketFeed exposing the latest NIFTY spot LTP for tick-driven exit
checks between the 60s REST poll cycles. Best-effort by design: any
connect/parse failure is caught so the caller can fall back to REST-only
polling instead of crashing the main loop.
"""
import unittest
from unittest.mock import MagicMock, patch

from fetchers.tick_feed import TickFeed


class TestTickFeedTickHandling(unittest.TestCase):

    def setUp(self):
        self.feed = TickFeed()

    def test_get_latest_price_none_before_any_tick(self):
        self.assertIsNone(self.feed.get_latest_price())

    def test_is_active_false_before_start(self):
        self.assertFalse(self.feed.is_active)

    def test_on_ticks_updates_latest_price(self):
        self.feed._on_ticks(MagicMock(), {"type": "Ticker Data", "LTP": "24010.50"})
        self.assertEqual(self.feed.get_latest_price(), 24010.50)

    def test_on_ticks_ignores_missing_ltp(self):
        self.feed._on_ticks(MagicMock(), {"type": "Ticker Data"})
        self.assertIsNone(self.feed.get_latest_price())

    def test_on_ticks_ignores_non_numeric_ltp(self):
        self.feed._on_ticks(MagicMock(), {"LTP": "not-a-number"})
        self.assertIsNone(self.feed.get_latest_price())

    def test_on_ticks_ignores_non_dict_payload(self):
        self.feed._on_ticks(MagicMock(), "garbage")
        self.assertIsNone(self.feed.get_latest_price())

    def test_on_ticks_latest_wins(self):
        self.feed._on_ticks(MagicMock(), {"LTP": "24000.00"})
        self.feed._on_ticks(MagicMock(), {"LTP": "24005.25"})
        self.assertEqual(self.feed.get_latest_price(), 24005.25)

    def test_on_error_logs_without_raising(self):
        self.feed._on_error(MagicMock(), Exception("ws dropped"))


class TestTickFeedLifecycle(unittest.TestCase):

    def setUp(self):
        self.feed = TickFeed()

    @patch('fetchers.tick_feed.settings')
    @patch('fetchers.tick_feed.MarketFeed')
    @patch('fetchers.tick_feed.DhanContext')
    def test_start_success(self, mock_context_cls, mock_feed_cls, mock_settings):
        mock_settings.dhan_client_id = "cid"
        mock_settings.dhan_access_token = "tok"
        mock_settings.security_id = "13"
        mock_feed_instance = MagicMock()
        mock_feed_cls.return_value = mock_feed_instance

        result = self.feed.start()

        self.assertTrue(result)
        self.assertTrue(self.feed.is_active)
        mock_feed_instance.start.assert_called_once()

    @patch('fetchers.tick_feed.settings')
    @patch('fetchers.tick_feed.MarketFeed')
    @patch('fetchers.tick_feed.DhanContext')
    def test_start_failure_is_caught_and_returns_false(self, mock_context_cls, mock_feed_cls, mock_settings):
        mock_context_cls.side_effect = Exception("bad creds")

        result = self.feed.start()

        self.assertFalse(result)
        self.assertFalse(self.feed.is_active)

    def test_stop_is_safe_when_never_started(self):
        self.feed.stop()  # must not raise
        self.assertFalse(self.feed.is_active)

    @patch('fetchers.tick_feed.settings')
    @patch('fetchers.tick_feed.MarketFeed')
    @patch('fetchers.tick_feed.DhanContext')
    def test_stop_closes_connection_and_clears_feed(self, mock_context_cls, mock_feed_cls, mock_settings):
        mock_settings.dhan_client_id = "cid"
        mock_settings.dhan_access_token = "tok"
        mock_settings.security_id = "13"
        mock_feed_instance = MagicMock()
        mock_feed_cls.return_value = mock_feed_instance
        self.feed.start()

        self.feed.stop()

        mock_feed_instance.close_connection.assert_called_once()
        self.assertFalse(self.feed.is_active)

    @patch('fetchers.tick_feed.settings')
    @patch('fetchers.tick_feed.MarketFeed')
    @patch('fetchers.tick_feed.DhanContext')
    def test_stop_swallows_close_connection_errors(self, mock_context_cls, mock_feed_cls, mock_settings):
        mock_settings.dhan_client_id = "cid"
        mock_settings.dhan_access_token = "tok"
        mock_settings.security_id = "13"
        mock_feed_instance = MagicMock()
        mock_feed_instance.close_connection.side_effect = Exception("already closed")
        mock_feed_cls.return_value = mock_feed_instance
        self.feed.start()

        self.feed.stop()  # must not raise

        self.assertFalse(self.feed.is_active)

    @patch('fetchers.tick_feed.settings')
    @patch('fetchers.tick_feed.MarketFeed')
    @patch('fetchers.tick_feed.DhanContext')
    def test_subscribes_to_configured_security_id_in_ticker_mode(self, mock_context_cls, mock_feed_cls, mock_settings):
        mock_settings.dhan_client_id = "cid"
        mock_settings.dhan_access_token = "tok"
        mock_settings.security_id = "13"
        mock_feed_cls.Ticker = 15
        mock_feed_cls.IDX = 0
        mock_feed_instance = MagicMock()
        mock_feed_cls.return_value = mock_feed_instance

        self.feed.start()

        _, kwargs = mock_feed_cls.call_args
        instruments = mock_feed_cls.call_args.args[1] if len(mock_feed_cls.call_args.args) > 1 else kwargs.get("instruments")
        self.assertIn((0, 13, 15), instruments)


if __name__ == "__main__":
    unittest.main()
