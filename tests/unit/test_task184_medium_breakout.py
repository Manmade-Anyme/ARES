"""
TASK-184 — restore MEDIUM-confidence failed-breakout signals.

DELIBERATE PRODUCT DECISION, NOT A BUG. TASK-172 raised
`breakout_failure_min_score` 2 -> 3, which made the engine fire only
HIGH-confidence (score >= 3/4 == 60%) failed breakouts and silenced every
MEDIUM (score 2/4 == 50%) one. ARES is used as a *manual-trading confirmation
aid*, not only an autotrader: a MEDIUM breakout that agrees with the trader's
open position is useful confidence, and one that disagrees is a useful prompt to
reconsider. A silent detector provides neither. The signal already carries a
`confidence` field ("MEDIUM"/"HIGH") so the trader can weight it themselves — the
engine must not pre-suppress the MEDIUM tier on their behalf.

These tests pin the restored behavior: a score-2 failed breakout fires and is
labeled MEDIUM. Do not "fix" this back to min_score=3 — see CHANGELOG TASK-184
and the config_profiles.py comment on `breakout_failure_min_score`.
"""
import unittest
from datetime import datetime

from config import settings
from config_profiles import NON_EXPIRY_CONFIG, EXPIRY_CONFIG
from models import OHLCVCandle, ResistanceLevel, SetupType, Direction
from detectors.breakout import FailedBreakoutDetector


class TestMediumBreakoutFires(unittest.TestCase):
    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)
        self.detector = FailedBreakoutDetector()
        self.levels = [
            ResistanceLevel(price=24000.0, source="PDL", strength=3),
            ResistanceLevel(price=24100.0, source="PDH", strength=3),
        ]

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def _arm_upside_breakout(self, volume=50000):
        # candle crosses above the 24100 level -> arms the breakout state
        candle1 = OHLCVCandle(
            timestamp=datetime.now(), open=24090.0, high=24120.0,
            low=24080.0, close=24110.0, volume=volume,
        )
        self.detector.update(candle1, 100000.0, 5.0, 100, 100, 100, 100, self.levels)
        self.assertIsNotNone(self.detector.active)

    def test_min_score_is_two(self):
        """The MEDIUM tier is live: the firing bar is 2 of 4, not 3."""
        self.assertEqual(settings.breakout_failure_min_score, 2)

    def test_score_two_breakout_fires_medium(self):
        """weak_volume(1) + deep_close(1) = 2/4 -> fires, labeled MEDIUM.

        Under the old min_score=3 this returned None (the exact regression that
        silenced the detector). It must now produce a live MEDIUM signal.
        """
        self._arm_upside_breakout(volume=50000)
        # avg_volume 100000 -> breakout candle (50000) IS weak (< 0.75*avg)      -> +1
        # close 24090 is 10pt back below level 24100 (>= deep_close 5pt)          -> +1
        # iv flat (0 > -3)                                                        -> 0
        # ATM CE OI unchanged (0% < 10% writers_active)                           -> 0
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24110.0, high=24115.0,
            low=24080.0, close=24090.0, volume=40000,
        )
        signal = self.detector.update(candle2, 100000.0, 0.0, 100, 100, 100, 100, self.levels)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.setup_type, SetupType.FAILED_BREAKOUT)
        self.assertEqual(signal.direction, Direction.BEARISH)  # failed up-break -> fade short
        self.assertEqual(signal.confidence, "MEDIUM")

    def test_score_one_still_rejected(self):
        """The gate still filters noise: deep_close alone (1/4) does not fire."""
        self._arm_upside_breakout(volume=50000)
        # avg_volume 10000 -> breakout candle (50000) NOT weak; iv flat; OI flat;
        # only deep_close true -> score 1 < 2 -> rejected
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24110.0, high=24115.0,
            low=24080.0, close=24090.0, volume=40000,
        )
        signal = self.detector.update(candle2, 10000.0, 0.0, 100, 100, 100, 100, self.levels)
        self.assertIsNone(signal)

    def test_expiry_profile_also_medium(self):
        """Both profiles inherit the class default -> MEDIUM is live on expiry too."""
        settings.apply_profile(EXPIRY_CONFIG)
        self.assertEqual(settings.breakout_failure_min_score, 2)


if __name__ == "__main__":
    unittest.main()
