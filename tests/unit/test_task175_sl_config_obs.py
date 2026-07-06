"""
Tests for TASK-175:

1. SL buffer removal — all 3 detectors place the stop exactly at the
   structural reference (breakout level / OI wall strike / exhaustion candle
   extreme). The `*_stop_buffer` config fields are deleted.
2. Hardcoded-values audit — every tunable numeric moves into config_profiles:
   breakout deep-close points, structural-target selection distances, OI wall
   conviction multiplier + wick scoring range, exhaustion extreme-volume
   factor / level proximity / volume history size, position-manager dedupe
   tolerance, engine IV-crush minimum samples. The dead
   `breakout_resistance_proximity` field (defined but never read) is removed.
3. Observation-only Discord alerts are restyled: OBSERVATION ONLY title,
   spot-level SL/targets retained for context, but no entry zone or option
   sizing fields (lots / option entry / option SL / option target), so they
   cannot be mistaken for
   tradeable signals (motivated by signal #2056 being traded manually).
"""
import asyncio
import dataclasses
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from config import settings
from config_profiles import TuningConfig, NON_EXPIRY_CONFIG, EXPIRY_CONFIG
from models import (
    AresSignal, OHLCVCandle, ResistanceLevel, SetupType, Direction,
)
from detectors.breakout import FailedBreakoutDetector
from detectors.oi_wall import OIWallDetector
from detectors.exhaustion import ExhaustionDetector
from alerts import send_discord


TUNING_FIELDS = {f.name for f in dataclasses.fields(TuningConfig)}


class TestConfigFields(unittest.TestCase):
    """New knobs exist; stop-buffer and dead fields are gone."""

    def test_stop_buffer_fields_removed(self):
        for name in ("breakout_stop_buffer", "oi_wall_stop_buffer",
                     "exhaustion_stop_buffer"):
            self.assertNotIn(name, TUNING_FIELDS)

    def test_dead_resistance_proximity_removed(self):
        self.assertNotIn("breakout_resistance_proximity", TUNING_FIELDS)

    def test_new_fields_present_with_defaults(self):
        self.assertEqual(NON_EXPIRY_CONFIG.breakout_deep_close_pts, 5.0)
        self.assertEqual(NON_EXPIRY_CONFIG.structural_target_min_distance_pts, 20.0)
        self.assertEqual(NON_EXPIRY_CONFIG.target_1_fallback_min_pts, 15.0)
        self.assertEqual(NON_EXPIRY_CONFIG.target_2_fallback_min_pts, 30.0)
        self.assertEqual(NON_EXPIRY_CONFIG.oi_wall_conviction_multiplier, 1.5)
        self.assertEqual(NON_EXPIRY_CONFIG.oi_wall_wick_min_range_pts, 2.0)
        self.assertEqual(NON_EXPIRY_CONFIG.exhaustion_extreme_volume_factor, 1.5)
        self.assertEqual(NON_EXPIRY_CONFIG.exhaustion_level_proximity_pts, 10.0)
        self.assertEqual(NON_EXPIRY_CONFIG.exhaustion_volume_history_size, 20)
        self.assertEqual(NON_EXPIRY_CONFIG.trade_dedupe_tolerance_pts, 1.0)
        self.assertEqual(NON_EXPIRY_CONFIG.iv_crush_min_samples, 10)
        # Expiry profile carries the same structural defaults
        self.assertEqual(EXPIRY_CONFIG.breakout_deep_close_pts, 5.0)


class BreakoutHarness(unittest.TestCase):
    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)
        self.detector = FailedBreakoutDetector()
        self.levels = [
            ResistanceLevel(price=24000.0, source="PDL", strength=3),
            ResistanceLevel(price=24100.0, source="PDH", strength=3),
        ]

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def _fire_bearish_signal(self):
        """Upward breakout over 24100 then deep close-back with weak volume,
        IV crush and 12% OI growth → signal fires."""
        candle1 = OHLCVCandle(
            timestamp=datetime.now(), open=24090.0, high=24120.0,
            low=24080.0, close=24110.0, volume=50000
        )
        self.detector.update(candle1, 100000.0, 5.0, 100, 100, 100, 100, self.levels)
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24110.0, high=24115.0,
            low=24080.0, close=24090.0, volume=40000
        )
        return self.detector.update(
            candle2, 100000.0, -15.0, 112, 100, 100, 100, self.levels
        )


class TestBreakoutSLAtLevel(BreakoutHarness):
    def test_bearish_sl_is_exactly_the_level(self):
        signal = self._fire_bearish_signal()
        self.assertIsNotNone(signal)
        self.assertEqual(signal.stop_loss, 24100.0)  # no +25 buffer


class TestDeepCloseConfigurable(BreakoutHarness):
    def test_raised_deep_close_threshold_drops_the_point(self):
        """With deep-close needing 50pts, a 10pt close-back scores only
        weak_volume + writers_active = 2 < 3 → no signal (IV flat)."""
        settings.apply_profile(dataclasses.replace(
            NON_EXPIRY_CONFIG, breakout_deep_close_pts=50.0
        ))
        candle1 = OHLCVCandle(
            timestamp=datetime.now(), open=24090.0, high=24120.0,
            low=24080.0, close=24110.0, volume=50000
        )
        self.detector.update(candle1, 100000.0, 5.0, 100, 100, 100, 100, self.levels)
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24110.0, high=24115.0,
            low=24080.0, close=24090.0, volume=40000
        )
        signal = self.detector.update(
            candle2, 100000.0, 0.0, 112, 100, 100, 100, self.levels
        )
        self.assertIsNone(signal)


class TestOIWallSLAtStrike(unittest.TestCase):
    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def test_bearish_sl_is_exactly_the_strike(self):
        candle = OHLCVCandle(
            timestamp=datetime.now(), open=24095.0, high=24105.0,
            low=24090.0, close=24092.0, volume=1000
        )
        wall = {
            "strike": 24100, "ce_oi": 6000000, "ce_oi_prev": 5900000,
            "ce_oi_change_pct": 2.0, "pe_oi": 1000000, "pe_oi_prev": 1000000,
            "pe_oi_change_pct": 0.0,
        }
        signal = OIWallDetector()._build_signal(
            candle=candle, spot=24090.0, wall=wall,
            direction=Direction.BEARISH, option_type="PE", levels=[]
        )
        self.assertEqual(signal.stop_loss, 24100.0)  # no +25 buffer


class TestExhaustionSLAtCandleExtreme(unittest.TestCase):
    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def test_bearish_sl_is_exactly_the_candle_high(self):
        detector = ExhaustionDetector()
        for _ in range(20):
            detector.volume_history.append(100000)
        candle = OHLCVCandle(
            timestamp=datetime.now(), open=24150.0, high=24160.0,
            low=24140.0, close=24155.0, volume=1000000
        )
        signal = detector.update(
            candle=candle, iv_current=20.0, iv_prev=15.0,
            levels=[ResistanceLevel(price=24000.0, source="PDL", strength=3)]
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.stop_loss, 24160.0)  # no +20 buffer

    def test_volume_history_size_from_config(self):
        settings.apply_profile(dataclasses.replace(
            NON_EXPIRY_CONFIG, exhaustion_volume_history_size=7
        ))
        detector = ExhaustionDetector()
        self.assertEqual(detector.volume_history.maxlen, 7)


def _obs_signal(alert_only: bool) -> AresSignal:
    return AresSignal(
        setup_type=SetupType.FAILED_BREAKOUT,
        direction=Direction.BEARISH,
        trigger_price=24350.0,
        entry_zone=(24339.35, 24349.35),
        stop_loss=24375.0,
        target_1=24252.35,
        target_2=24250.0,
        confidence="HIGH",
        reasons=["Counter-trend vs VWAP/PDH-PDL regime — observation only"],
        timestamp=datetime.now(),
        strike_to_trade=24350,
        option_type="PE",
        alert_only=alert_only,
    )


class TestObservationAlertRestyle(unittest.IsolatedAsyncioTestCase):
    """alert_only signals must be visually unmistakable: loud title, spot-level
    SL/targets kept for context, but no entry zone or option sizing card."""

    def _post_payload(self, mock_client):
        args, kwargs = mock_client.post.call_args
        return kwargs.get("json") or args[1]

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_observation_alert_has_loud_title_sl_targets_no_entry_or_sizing(
            self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_settings.nifty_lot_size = 65
        mock_client = AsyncMock()
        mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await send_discord(_obs_signal(alert_only=True), 24344.35)

        payload = self._post_payload(mock_client)
        embed = payload["embeds"][0]
        self.assertIn("OBSERVATION ONLY", embed["title"])
        field_names = [f["name"] for f in embed["fields"]]
        # Spot-level SL and targets stay in the alert for context
        self.assertTrue(any("🛑 SL" in n for n in field_names))
        self.assertTrue(any("🎯 Target" in n for n in field_names))
        # ...but no entry zone or option sizing card
        for banned in ("✅ Entry", "🔢 Lots", "Option Entry",
                       "Option SL", "Option Target", "Sizing"):
            self.assertFalse(
                any(banned in n for n in field_names),
                f"observation alert must not contain {banned!r}"
            )
        self.assertTrue(any("Reasons" in n for n in field_names))

    @patch('alerts.settings')
    @patch('httpx.AsyncClient')
    async def test_tradeable_alert_unchanged(self, mock_client_class, mock_settings):
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_settings.nifty_lot_size = 65
        mock_client = AsyncMock()
        mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await send_discord(_obs_signal(alert_only=False), 24344.35)

        payload = self._post_payload(mock_client)
        embed = payload["embeds"][0]
        self.assertNotIn("OBSERVATION", embed["title"])
        field_names = [f["name"] for f in embed["fields"]]
        self.assertTrue(any("🛑 SL" in n for n in field_names))


if __name__ == "__main__":
    unittest.main()
