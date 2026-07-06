"""
Tests for TASK-174 breakout OI scoring fix:

- The writers_active OI-growth threshold moves from a hardcoded 3.0% into
  config (`breakout_writers_active_min_pct`), defaulting to 10.0% on normal
  days and 15.0% on expiry days. Intraminute ATM OI drift of 3-5% is common
  noise and was awarding a near-free point.
- writers_holding (OI >= previous OI, including zero change) is no longer a
  scored condition — any OI growth >= the active threshold already implies
  "holding", so scoring both double-counted a single OI reading. It remains
  a reason string for signal context only.
- The score matrix shrinks from 5 to 4 conditions:
  weak_volume, iv_falling, writers_active, deep_close.
  breakout_failure_min_score stays 3 → a signal now needs 3 of 4 real
  conditions instead of 3 of 5 (where 2 could come from one OI reading).
"""
import unittest
from datetime import datetime

from config import settings
from config_profiles import NON_EXPIRY_CONFIG, EXPIRY_CONFIG
from models import OHLCVCandle, ResistanceLevel
from detectors.breakout import FailedBreakoutDetector


class TestWritersActiveThresholdConfig(unittest.TestCase):
    """Threshold lives in config_profiles, not hardcoded in the detector."""

    def test_non_expiry_default_is_ten_percent(self):
        self.assertEqual(NON_EXPIRY_CONFIG.breakout_writers_active_min_pct, 10.0)

    def test_expiry_profile_is_fifteen_percent(self):
        self.assertEqual(EXPIRY_CONFIG.breakout_writers_active_min_pct, 15.0)


class BreakoutScoringHarness(unittest.TestCase):
    """Shared setup: an upward breakout over 24100 that closes back deeply."""

    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)
        self.detector = FailedBreakoutDetector()
        self.levels = [
            ResistanceLevel(price=24000.0, source="PDL", strength=3),
            ResistanceLevel(price=24100.0, source="PDH", strength=3),
        ]

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def _breakout_up(self):
        candle1 = OHLCVCandle(
            timestamp=datetime.now(), open=24090.0, high=24120.0,
            low=24080.0, close=24110.0, volume=50000
        )
        self.detector.update(candle1, 100000.0, 5.0, 100, 100, 100, 100, self.levels)
        self.assertIsNotNone(self.detector.active)

    def _fail_back(self, ce_oi: int, ce_oi_prev: int, iv_change: float = 0.0,
                   avg_volume: float = 100000.0):
        """Close back deeply (10 pts) below the level. With avg_volume=100000
        the breakout candle (50000) is weak → weak_volume + deep_close = 2
        baseline points; the OI args control the third."""
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24110.0, high=24115.0,
            low=24080.0, close=24090.0, volume=40000
        )
        return self.detector.update(
            candle2, avg_volume, iv_change, ce_oi, ce_oi_prev, 100, 100, self.levels
        )


class TestWritersHoldingNotScored(BreakoutScoringHarness):
    """OI merely holding (or tiny growth) no longer contributes a point."""

    def test_flat_oi_with_two_conditions_does_not_fire(self):
        """weak_volume + deep_close + flat OI (holding only) = 2 < 3 → rejected.
        Pre-TASK-174 this fired: holding gave a free 3rd point."""
        self._breakout_up()
        signal = self._fail_back(ce_oi=100, ce_oi_prev=100)
        self.assertIsNone(signal)

    def test_five_percent_growth_below_threshold_does_not_fire(self):
        """5% OI growth < 10% threshold → not active → 2 < 3 → rejected.
        Pre-TASK-174 the 3% hardcoded bar made this fire with score 4."""
        self._breakout_up()
        signal = self._fail_back(ce_oi=105, ce_oi_prev=100)
        self.assertIsNone(signal)


class TestWritersActiveScored(BreakoutScoringHarness):
    """Genuine writer defense (>= configured %) still scores and fires."""

    def test_twelve_percent_growth_fires_high(self):
        """weak_volume + deep_close + writers_active(12%) = 3 of 4 → HIGH (75%)."""
        self._breakout_up()
        signal = self._fail_back(ce_oi=112, ce_oi_prev=100)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.confidence, "HIGH")

    def test_threshold_boundary_inclusive(self):
        """Exactly 10.0% growth counts as active."""
        self._breakout_up()
        signal = self._fail_back(ce_oi=110, ce_oi_prev=100)
        self.assertIsNotNone(signal)

    def test_reason_string_reflects_configured_threshold(self):
        """Discord reason shows the config value, not a stale hardcoded 3%."""
        self._breakout_up()
        signal = self._fail_back(ce_oi=112, ce_oi_prev=100)
        self.assertIsNotNone(signal)
        active_reasons = [r for r in signal.reasons if "OI growth" in r]
        self.assertEqual(len(active_reasons), 1)
        self.assertIn("10.0%", active_reasons[0])
        self.assertNotIn("3%", active_reasons[0])

    def test_holding_still_reported_as_reason_context(self):
        """writers_holding stays visible in reasons even though unscored."""
        self._breakout_up()
        signal = self._fail_back(ce_oi=112, ce_oi_prev=100)
        self.assertIsNotNone(signal)
        self.assertTrue(any("did not cover" in r for r in signal.reasons))


if __name__ == "__main__":
    unittest.main()
