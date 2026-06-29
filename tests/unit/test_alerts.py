import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta
import httpx
from models import AresSignal, SetupType, Direction
from alerts import format_signal, send_discord, send_startup_alert, send_error_alert, send_trade_update

class TestAlerts(unittest.IsolatedAsyncioTestCase):

    @patch('alerts.datetime')
    def test_format_signal_without_sizing(self, mock_datetime):
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
            reasons=["Reason 1"],
            timestamp=fixed_dt,
            strike_to_trade=23000,
            option_type="CE",
            signal_id="1234"
        )
        
        msg = format_signal(signal, spot=23005.0)
        self.assertIn("🕒 Time  : 16-Jun-2026 12:30:45 IST", msg)
        self.assertIn("📍 Spot  : 23005.00", msg)
        self.assertIn("✅ Entry : **22950.00 - 23050.00**", msg)
        self.assertIn("🛑 SL    : **22900.00 **(Spot Ref)", msg)
        self.assertIn("🎯 Target: **T1=23100.00 | T2=23200.00**", msg)
        self.assertIn("⚡ Trade : **23000 CE**", msg)
        self.assertIn("⭐ Confidence : HIGH", msg)
        self.assertNotIn("Option Sizing Calculator", msg)

    @patch('alerts.datetime')
    def test_format_signal_with_sizing(self, mock_datetime):
        ist_tz = timezone(timedelta(hours=5, minutes=30))
        fixed_dt = datetime(2026, 6, 16, 12, 30, 45, tzinfo=ist_tz)
        mock_datetime.now.return_value = fixed_dt

        signal = AresSignal(
            setup_type=SetupType.OI_WALL_REJECTION,
            direction=Direction.BEARISH,
            trigger_price=24000.0,
            entry_zone=(23990.0, 24010.0),
            stop_loss=24025.0,
            target_1=23950.0,
            target_2=23900.0,
            confidence="MEDIUM",
            reasons=["Reason 1"],
            timestamp=fixed_dt,
            strike_to_trade=24000,
            option_type="PE",
            signal_id="5678"
        )
        # Sizing params
        signal.suggested_lots = 2
        signal.option_premium = 85.0
        signal.option_delta = -0.48
        signal.option_sl = 65.0
        signal.option_target = 110.0
        signal.risk_pct = 1.5
        
        msg = format_signal(signal, spot=24001.0)
        self.assertIn("📐 Option Sizing Calculator (Risk: 1.5%) -", msg)
        self.assertIn("🔢 Lots   : **2** (Nifty Lot Size: 65)", msg)
        self.assertIn("✅ Entry : **₹ 85.00** (Delta: -0.4800)", msg)
        self.assertIn("🛑 SL  : **₹ 65.00**", msg)
        self.assertIn("🎯 Target : **₹ 110.00**", msg)

    @patch('alerts.settings')
    async def test_send_discord_no_webhook(self, mock_settings):
        mock_settings.discord_webhook_url = ""
        # Should return early
        with patch('httpx.AsyncClient') as mock_client:
            await send_discord(MagicMock(), 23000.0)
            mock_client.assert_not_called()

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_send_discord_success(self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        signal = AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=Direction.BULLISH,
            trigger_price=23000.0,
            entry_zone=(22950.0, 23050.0),
            stop_loss=22900.0,
            target_1=23100.0,
            target_2=23200.0,
            confidence="HIGH",
            reasons=["Reason 1"],
            timestamp=datetime.now(),
            strike_to_trade=23000,
            option_type="CE"
        )

        await send_discord(signal, 23000.0)
        mock_client.post.assert_called_once()

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_send_discord_exception_safety(self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.HTTPStatusError("Error", request=MagicMock(), response=MagicMock())
        mock_client_class.return_value.__aenter__.return_value = mock_client

        signal = AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=Direction.BULLISH,
            trigger_price=23000.0,
            entry_zone=(22950.0, 23050.0),
            stop_loss=22900.0,
            target_1=23100.0,
            target_2=23200.0,
            confidence="HIGH",
            reasons=["Reason 1"],
            timestamp=datetime.now(),
            strike_to_trade=23000,
            option_type="CE"
        )

        # Exception should be caught and not raised
        await send_discord(signal, 23000.0)
        mock_client.post.assert_called_once()

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_send_startup_alert_exception_safety(self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Network down")
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await send_startup_alert(pdh=24000.0, pdl=23900.0, ml_active=False)
        mock_client.post.assert_called_once()

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_send_startup_alert_success(self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await send_startup_alert(pdh=24000.0, pdl=23900.0, ml_active=True)
        mock_client.post.assert_called_once()

    @patch('alerts.settings')
    async def test_send_error_alert_no_webhook(self, mock_settings):
        mock_settings.discord_health_webhook_url = ""
        mock_settings.discord_webhook_url = ""
        with patch('httpx.AsyncClient') as mock_client:
            await send_error_alert("Database error")
            mock_client.assert_not_called()

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_send_error_alert_success(self, mock_client_class, mock_settings):
        # Uses standard webhook fallback
        mock_settings.discord_health_webhook_url = ""
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await send_error_alert("Database error")
        mock_client.post.assert_called_once()

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_send_error_alert_exception_safety(self, mock_client_class, mock_settings):
        mock_settings.discord_health_webhook_url = "http://mock-health"
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Webhook post fail")
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await send_error_alert("Database error")
        mock_client.post.assert_called_once()

    @patch('alerts.settings')
    async def test_send_trade_update_no_webhook(self, mock_settings):
        mock_settings.discord_webhook_url = ""
        with patch('httpx.AsyncClient') as mock_client:
            await send_trade_update({}, 23000.0, "T1_HIT")
            mock_client.assert_not_called()

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_send_trade_update_variations(self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        # Bearish, T2 Hit
        trade_bearish = {
            "signal_id": "123",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BEARISH",
            "entry_price": 24000.0,
            "stop_loss": 24025.0,
            "state": "T1_HIT"
        }
        await send_trade_update(trade_bearish, spot=23900.0, update_type="T2_HIT")
        mock_client.post.assert_called_once()

        # Bearish, T1 Hit
        mock_client.reset_mock()
        await send_trade_update(trade_bearish, spot=23950.0, update_type="T1_HIT")
        mock_client.post.assert_called_once()

        # SL Hit (trailing stop at entry)
        mock_client.reset_mock()
        trade_trailing_sl = {
            "signal_id": "123",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 24000.0,
            "state": "T1_HIT"
        }
        await send_trade_update(trade_trailing_sl, spot=24000.0, update_type="SL_HIT")
        mock_client.post.assert_called_once()

        # Regular SL Hit
        mock_client.reset_mock()
        trade_regular_sl = {
            "signal_id": "123",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "state": "OPEN"
        }
        await send_trade_update(trade_regular_sl, spot=23975.0, update_type="SL_HIT")
        mock_client.post.assert_called_once()

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_send_trade_update_exception_safety(self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Webhook post fail")
        mock_client_class.return_value.__aenter__.return_value = mock_client

        trade = {
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "state": "OPEN",
            "setup_type": "FAILED_BREAKOUT"
        }
        await send_trade_update(trade, spot=23975.0, update_type="SL_HIT")
        mock_client.post.assert_called_once()

    @patch('alerts.settings')
    async def test_send_startup_alert_no_webhook(self, mock_settings):
        mock_settings.discord_webhook_url = ""
        with patch('httpx.AsyncClient') as mock_client:
            await send_startup_alert(pdh=24000.0, pdl=23900.0, ml_active=False)
            mock_client.assert_not_called()

if __name__ == '__main__':
    unittest.main()
