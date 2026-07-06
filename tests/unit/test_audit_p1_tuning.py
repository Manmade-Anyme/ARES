"""
Tests for TASK-172 audit P1 tuning:
- Item 8: breakout_failure_min_score raised 2→3 and closed_back excluded from
  the failure score (it stays a hard requirement, not a scored condition).
- Item 10: anti-IV-crush filter exempts HIGH confidence, applies symmetrically
  (bullish→CE IV, bearish→PE IV) and uses a longer configurable lookback.
- Item 12: speed-filter window/threshold move into config_profiles; confidence
  bars standardized at >=60% of each detector's score matrix.
"""
import dataclasses
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime

from config import settings
from config_profiles import TuningConfig, NON_EXPIRY_CONFIG
from engine import AresEngine
from models import (
    OHLCVCandle, ATMStrikes, AresSignal, SetupType, Direction,
    ResistanceLevel, confidence_from_score,
)
from detectors.breakout import FailedBreakoutDetector
from detectors.oi_wall import OIWallDetector
from detectors.exhaustion import ExhaustionDetector


class TestConfidenceHelper(unittest.TestCase):
    """Item 12: one shared confidence band — HIGH at >=60% of the matrix."""

    def test_confidence_bands_at_60_percent(self):
        self.assertEqual(confidence_from_score(3, 5), "HIGH")    # exactly 60%
        self.assertEqual(confidence_from_score(2, 5), "MEDIUM")  # 40%
        self.assertEqual(confidence_from_score(3, 4), "HIGH")    # 75%
        self.assertEqual(confidence_from_score(2, 4), "MEDIUM")  # 50% — was HIGH pre-TASK-172
        self.assertEqual(confidence_from_score(0, 4), "MEDIUM")

    def test_confidence_zero_matrix_is_medium(self):
        self.assertEqual(confidence_from_score(0, 0), "MEDIUM")


class TestBreakoutScoreExcludesClosedBack(unittest.TestCase):
    """Item 8: closed_back is a hard gate, no longer a scored condition."""

    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)
        self.detector = FailedBreakoutDetector()
        self.levels = [
            ResistanceLevel(price=24000.0, source="PDL", strength=3),
            ResistanceLevel(price=24100.0, source="PDH", strength=3),
        ]

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def _breakout_up(self, volume=50000):
        candle1 = OHLCVCandle(
            timestamp=datetime.now(), open=24090.0, high=24120.0,
            low=24080.0, close=24110.0, volume=volume
        )
        self.detector.update(candle1, 100000.0, 5.0, 100, 100, 100, 100, self.levels)
        self.assertIsNotNone(self.detector.active)

    def test_min_score_default_raised_to_three(self):
        self.assertEqual(settings.breakout_failure_min_score, 3)

    def test_closed_back_plus_two_conditions_no_longer_fires(self):
        """Old scoring: closed_back(1) + writers_holding(1) + deep_close(1) = 3 → fired.
        New scoring: writers_holding(1) + deep_close(1) = 2 < 3 → rejected."""
        self._breakout_up()
        # avg volume low → breakout volume NOT weak; IV flat; OI unchanged (holding, not active)
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24110.0, high=24115.0,
            low=24080.0, close=24090.0, volume=40000
        )
        signal = self.detector.update(candle2, 10000.0, 0.0, 100, 100, 100, 100, self.levels)
        self.assertIsNone(signal)

    def test_three_real_conditions_fires_high(self):
        """writers_active + weak_volume + deep_close = 3/4 → fires, HIGH at 60%.
        (TASK-174: writers_holding unscored, so the third point comes from
        genuine OI growth past the 10% threshold.)"""
        self._breakout_up()
        # avg 100000 → breakout volume 50000 weak; IV flat; CE OI +12% (active)
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24110.0, high=24115.0,
            low=24080.0, close=24090.0, volume=40000
        )
        signal = self.detector.update(candle2, 100000.0, 0.0, 112, 100, 100, 100, self.levels)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.confidence, "HIGH")

    def test_shallow_close_back_with_weak_conditions_rejected(self):
        """closed_back alone (score 0-2) can never fire under the new gate."""
        self._breakout_up()
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24110.0, high=24115.0,
            low=24080.0, close=24099.0, volume=40000  # shallow: 1pt back
        )
        signal = self.detector.update(candle2, 10000.0, 0.0, 90, 100, 100, 100, self.levels)
        self.assertIsNone(signal)


class TestStandardizedConfidenceBars(unittest.TestCase):
    """Item 12: OI wall and exhaustion HIGH bar moves 2/4 → 3/4."""

    def test_oi_wall_two_of_four_is_now_medium(self):
        detector = OIWallDetector()
        # mag(6M >= 1.5x4M)=1, growth(2% < 7.5%)=0, pierced(high 24105 >= 24100)=1,
        # wick(upper wick 1pt < 40% of range)=0 → score 2 → MEDIUM (was HIGH)
        candle = OHLCVCandle(
            timestamp=datetime.now(), open=24095.0, high=24105.0,
            low=24090.0, close=24104.0, volume=1000
        )
        wall = {
            "strike": 24100, "ce_oi": 6000000, "ce_oi_prev": 5900000,
            "ce_oi_change_pct": 2.0, "pe_oi": 1000000, "pe_oi_prev": 1000000,
            "pe_oi_change_pct": 0.0,
        }
        signal = detector._build_signal(
            candle=candle, spot=24090.0, wall=wall,
            direction=Direction.BEARISH, option_type="PE", levels=[]
        )
        self.assertEqual(signal.confidence, "MEDIUM")

    def test_exhaustion_two_of_four_is_now_medium(self):
        detector = ExhaustionDetector()
        for _ in range(20):
            detector.volume_history.append(100000)
        # extreme_volume(1M >= 375000)=1, extreme_doji(body 25/range 115 = 0.217,
        # doji-like but not < 0.175)=0, iv_panic(flat)=0, near_level(24195 within
        # 10 of 24200)=1 → score 2 → MEDIUM (was HIGH)
        levels = [ResistanceLevel(price=24200.0, source="OI_WALL", strength=2)]
        candle = OHLCVCandle(
            timestamp=datetime.now(), open=24100.0, high=24195.0,
            low=24080.0, close=24125.0, volume=1000000
        )
        signal = detector.update(candle=candle, iv_current=15.0, iv_prev=15.0, levels=levels)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.confidence, "MEDIUM")


class TestEngineP1Filters(unittest.TestCase):
    """Items 10 & 12: engine-level filter changes."""

    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)
        self.engine = AresEngine()

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def _make_candle(self, close, volume=100000):
        candle = MagicMock()
        candle.high = close + 1.0
        candle.low = close - 1.0
        candle.close = close
        candle.open = close
        candle.volume = volume
        return candle

    def _make_atm(self, ce_iv=12.0, pe_iv=12.0):
        atm = MagicMock(spec=ATMStrikes)
        atm.ce = MagicMock()
        atm.ce.iv = ce_iv
        atm.pe = MagicMock()
        atm.pe.iv = pe_iv
        atm.spot_price = 24000.0
        return atm

    def _make_signal(self, confidence="MEDIUM", direction=Direction.BULLISH,
                     setup_type=SetupType.FAILED_BREAKOUT):
        opt_type = "CE" if direction == Direction.BULLISH else "PE"
        # R:R 2.0 both ways so the R:R gate never interferes
        if direction == Direction.BULLISH:
            stop_loss, target_1, target_2 = 23950.0, 24100.0, 24200.0
        else:
            stop_loss, target_1, target_2 = 24050.0, 23900.0, 23800.0
        return AresSignal(
            setup_type=setup_type,
            direction=direction,
            trigger_price=24000.0,
            entry_zone=(23990.0, 24010.0),
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            confidence=confidence,
            reasons=["Reason 1"],
            timestamp=datetime.now(),
            strike_to_trade=24000,
            option_type=opt_type,
        )

    def _trending_buffer(self):
        for i in range(15):
            self.engine.candle_buffer.append(self._make_candle(24000.0 + i * 3))

    def _tick_with(self, signal, atm):
        self._trending_buffer()
        self.engine.breakout_detector.update = MagicMock(return_value=signal)
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)
        return self.engine.tick(self._make_candle(24045.0), [], atm, 0.0, [])

    # ── Item 10: anti-IV-crush filter ────────────────────────────────────────

    def test_iv_lookback_size_comes_from_config(self):
        self.assertEqual(self.engine.iv_lookback.maxlen, settings.iv_crush_lookback_size)
        self.assertEqual(self.engine.pe_iv_lookback.maxlen, settings.iv_crush_lookback_size)
        self.assertEqual(settings.iv_crush_lookback_size, 60)

    def test_high_confidence_bullish_exempt_from_iv_crush(self):
        for _ in range(18):
            self.engine.iv_lookback.append(10.0)
        signal = self._make_signal(confidence="HIGH", direction=Direction.BULLISH)
        result = self._tick_with(signal, self._make_atm(ce_iv=15.0))
        self.assertIsNotNone(result)

    def test_medium_confidence_bullish_suppressed_on_high_ce_iv(self):
        for _ in range(18):
            self.engine.iv_lookback.append(10.0)
        signal = self._make_signal(confidence="MEDIUM", direction=Direction.BULLISH)
        result = self._tick_with(signal, self._make_atm(ce_iv=15.0))
        self.assertIsNone(result)

    def test_medium_confidence_bearish_suppressed_on_high_pe_iv(self):
        """Symmetric: bearish entries buy PEs, so top-percentile PE IV suppresses."""
        for _ in range(18):
            self.engine.pe_iv_lookback.append(10.0)
        signal = self._make_signal(confidence="MEDIUM", direction=Direction.BEARISH)
        result = self._tick_with(signal, self._make_atm(ce_iv=10.0, pe_iv=15.0))
        self.assertIsNone(result)

    def test_medium_confidence_bearish_passes_on_low_pe_iv(self):
        for _ in range(18):
            self.engine.pe_iv_lookback.append(12.0)
        signal = self._make_signal(confidence="MEDIUM", direction=Direction.BEARISH)
        result = self._tick_with(signal, self._make_atm(ce_iv=10.0, pe_iv=10.0))
        self.assertIsNotNone(result)

    def test_observation_only_exhaustion_survives_iv_crush_filter(self):
        """Alert-only signals are never traded — keep them for observation
        data. The exhaustion_alert_only gate itself (TASK-172) is unchanged
        by TASK-180's default flip to live; force it on here to test the
        mechanism directly rather than depend on the production default."""
        settings.apply_profile(dataclasses.replace(NON_EXPIRY_CONFIG, exhaustion_alert_only=True))
        for _ in range(18):
            self.engine.pe_iv_lookback.append(10.0)
        signal = self._make_signal(confidence="MEDIUM", direction=Direction.BEARISH,
                                   setup_type=SetupType.EXHAUSTION_REVERSAL)
        result = self._tick_with(signal, self._make_atm(ce_iv=10.0, pe_iv=15.0))
        self.assertIsNotNone(result)
        self.assertTrue(result.alert_only)

    # ── Item 12: speed filter from config ────────────────────────────────────

    def test_speed_filter_threshold_from_config(self):
        """Range-2 flat market suppresses at the default 15.0 threshold but
        passes when the profile lowers the threshold below the observed range."""
        settings.apply_profile(TuningConfig(speed_filter_min_range_pts=1.0))
        engine = AresEngine()
        for i in range(15):
            engine.candle_buffer.append(self._make_candle(24000.0 + (i % 2)))
        signal = self._make_signal(confidence="MEDIUM", direction=Direction.BULLISH)
        engine.breakout_detector.update = MagicMock(return_value=signal)
        engine.oi_wall_detector.update = MagicMock(return_value=None)
        engine.exhaustion_detector.update = MagicMock(return_value=None)
        result = engine.tick(self._make_candle(24001.0), [], self._make_atm(), 0.0, [])
        self.assertIsNotNone(result)

    def test_speed_filter_window_from_config(self):
        """With a 20-candle window configured and only 15 candles buffered,
        the filter stays inactive (not enough history)."""
        settings.apply_profile(TuningConfig(speed_filter_window_candles=20))
        engine = AresEngine()
        for i in range(15):
            engine.candle_buffer.append(self._make_candle(24000.0 + (i % 2)))
        signal = self._make_signal(confidence="MEDIUM", direction=Direction.BULLISH)
        engine.breakout_detector.update = MagicMock(return_value=signal)
        engine.oi_wall_detector.update = MagicMock(return_value=None)
        engine.exhaustion_detector.update = MagicMock(return_value=None)
        result = engine.tick(self._make_candle(24001.0), [], self._make_atm(), 0.0, [])
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
