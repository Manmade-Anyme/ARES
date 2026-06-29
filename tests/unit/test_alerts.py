import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta
from models import AresSignal, SetupType, Direction
from alerts import format_signal, send_trade_update, send_startup_alert

class TestAlerts(unittest.TestCase):

    @patch('alerts.datetime')
    def test_format_signal_includes_timestamp(self, mock_datetime):
        # Set expected IST time
        ist_tz = timezone(timedelta(hours=5, minutes=30))
        fixed_dt = datetime(2026, 6, 16, 12, 30, 45, tzinfo=ist_tz)
        mock_datetime.now.return_value = fixed_dt

        signal = AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=Direction.BULLISH,
            trigger_price=23000.0,
            entry_zone=(22950.0, 23050.0),
            stop_loss=22900.0,
            target_1=23100.0,
            target_2=23200.0,
            confidence="HIGH",
            reasons=["Reason 1", "Reason 2"],
            timestamp=fixed_dt,
            strike_to_trade=23000,
            option_type="CE",
            signal_id="1234"
        )
        
        msg = format_signal(signal, spot=23005.0)
        self.assertIn("🕒 Time  : 16-Jun-2026 12:30:45 IST", msg)



    @patch('alerts.settings')
    @patch('alerts.httpx.AsyncClient')
    @patch('alerts.datetime')
    def test_send_trade_update_includes_timestamp(self, mock_datetime, mock_client_class, mock_settings):
        # Set discord webhook URL to not be empty
        mock_settings.discord_webhook_url = "http://mock-webhook"

        ist_tz = timezone(timedelta(hours=5, minutes=30))
        fixed_dt = datetime(2026, 6, 16, 12, 30, 45, tzinfo=ist_tz)
        mock_datetime.now.return_value = fixed_dt

        # Mock httpx async client and response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        trade = {
            "signal_id": "1234",
            "setup_type": "FAILED_BREAKOUT",
            "direction": "BULLISH",
            "entry_price": 23000.0,
            "stop_loss": 22950.0,
            "state": "T1_HIT"
        }

        import asyncio
        asyncio.run(send_trade_update(trade, spot=23100.0, update_type="T1_HIT"))

        # Verify client posted to webhook with message containing timestamp
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        content = payload["content"]
        self.assertIn("🕒 Time    : 16-Jun-2026 12:30:45 IST", content)

    @patch('alerts.settings')
    @patch('alerts.httpx.AsyncClient')
    def test_send_startup_alert_with_ml_active(self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        import asyncio
        asyncio.run(send_startup_alert(pdh=24200.0, pdl=24000.0, profile_name="NON-EXPIRY", ml_active=True))

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        content = payload["content"]
        self.assertIn("ML Data Collection : ACTIVE", content)

    @patch('alerts.settings')
    @patch('alerts.httpx.AsyncClient')
    def test_send_startup_alert_with_ml_inactive(self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        import asyncio
        asyncio.run(send_startup_alert(pdh=24200.0, pdl=24000.0, profile_name="EXPIRY", ml_active=False))

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        content = payload["content"]
        self.assertIn("ML Data Collection : inactive", content)
        self.assertIn("EXPIRY DAY PROFILE", content)

    @patch('alerts.settings')
    @patch('alerts.httpx.AsyncClient')
    def test_send_startup_alert_skips_when_no_webhook(self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = ""

        import asyncio
        asyncio.run(send_startup_alert(pdh=24200.0, pdl=24000.0, ml_active=True))

        mock_client_class.assert_not_called()


if __name__ == '__main__':
    unittest.main()
