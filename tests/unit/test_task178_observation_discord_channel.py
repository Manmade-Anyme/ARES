"""
Tests for TASK-178: route observation-only (alert_only) signal alerts to a
separate, optional Discord channel so they don't spam the main trading
channel. Falls back to the main webhook when the observation webhook isn't
configured, so existing single-channel setups are unaffected.
"""
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime

from config import Secrets
from models import AresSignal, SetupType, Direction
from alerts import send_discord


def _signal(alert_only: bool, direction=Direction.BULLISH):
    return AresSignal(
        setup_type=SetupType.EXHAUSTION_REVERSAL,
        direction=direction,
        trigger_price=24000.0,
        entry_zone=(23990.0, 24010.0),
        stop_loss=23950.0,
        target_1=24100.0,
        target_2=24200.0,
        confidence="MEDIUM",
        reasons=["Reason 1"],
        timestamp=datetime.now(),
        strike_to_trade=24000,
        option_type="CE",
        signal_id="4242",
        alert_only=alert_only,
    )


class TestConfigField(unittest.TestCase):
    def test_observation_webhook_field_optional_and_defaults_none(self):
        # Field exists and is optional (None default) — same convention as
        # discord_health_webhook_url.
        self.assertIn("discord_observation_webhook_url", Secrets.model_fields)
        self.assertIsNone(Secrets.model_fields["discord_observation_webhook_url"].default)


class TestObservationChannelRouting(unittest.IsolatedAsyncioTestCase):

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_observation_signal_routes_to_observation_webhook_when_configured(
            self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://main-webhook"
        mock_settings.discord_observation_webhook_url = "http://observation-webhook"
        mock_client = AsyncMock()
        mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await send_discord(_signal(alert_only=True), 24000.0)

        args, kwargs = mock_client.post.call_args
        self.assertEqual(args[0], "http://observation-webhook")

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_observation_signal_falls_back_to_main_webhook_when_unconfigured(
            self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://main-webhook"
        mock_settings.discord_observation_webhook_url = ""
        mock_client = AsyncMock()
        mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await send_discord(_signal(alert_only=True), 24000.0)

        args, kwargs = mock_client.post.call_args
        self.assertEqual(args[0], "http://main-webhook")

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_tradeable_signal_always_uses_main_webhook(
            self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://main-webhook"
        mock_settings.discord_observation_webhook_url = "http://observation-webhook"
        mock_client = AsyncMock()
        mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await send_discord(_signal(alert_only=False), 24000.0)

        args, kwargs = mock_client.post.call_args
        self.assertEqual(args[0], "http://main-webhook")

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_observation_signal_exception_safety(self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://main-webhook"
        mock_settings.discord_observation_webhook_url = "http://observation-webhook"
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("network down")
        mock_client_class.return_value.__aenter__.return_value = mock_client

        # Must not raise even if the observation webhook is unreachable.
        await send_discord(_signal(alert_only=True), 24000.0)
