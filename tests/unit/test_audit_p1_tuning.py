"""
Tests for TASK-172 audit P1 tuning (the parts that survive TASK-182):
- Item 8: breakout_failure_min_score raised 2→3 and closed_back excluded from
  the failure score (it stays a hard requirement, not a scored condition).
- Item 12: confidence bars standardized at >=60% of each detector's score
  matrix.

The flat-market speed filter and the anti-IV-crush filter (items 10 & 12) were
removed in TASK-182; the final class asserts they no longer suppress signals.
"""
import unittest
from unittest.mock import MagicMock
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


class TestSuppressionFiltersRemoved(unittest.TestCase):
    """TASK-182: the flat-market speed filter and the anti-IV-crush filter are
    gone — a MEDIUM signal that they used to suppress is a live trade now."""

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

    def test_engine_keeps_no_iv_lookback(self):
        """The per-side IV lookbacks existed only for the anti-IV-crush filter."""
        self.assertFalse(hasattr(self.engine, "iv_lookback"))
        self.assertFalse(hasattr(self.engine, "pe_iv_lookback"))

    def test_medium_signal_in_dead_flat_market_now_trades(self):
        """Speed filter removed: a MEDIUM signal with a 0-point rolling range
        used to be suppressed — now it is a live trade."""
        for _ in range(15):
            self.engine.candle_buffer.append(self._make_candle(24000.0))  # flat
        self.engine.breakout_detector.update = MagicMock(
            return_value=self._make_signal(confidence="MEDIUM", direction=Direction.BULLISH))
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)
        result = self.engine.tick(self._make_candle(24000.0), [], self._make_atm(), 0.0, [])
        self.assertIsNotNone(result)

    def test_medium_bullish_on_top_percentile_ce_iv_now_trades(self):
        """Anti-IV-crush filter removed: a MEDIUM bullish signal with high CE
        IV used to be suppressed — now it trades."""
        signal = self._make_signal(confidence="MEDIUM", direction=Direction.BULLISH)
        result = self._tick_with(signal, self._make_atm(ce_iv=15.0))
        self.assertIsNotNone(result)

    def test_medium_bearish_on_top_percentile_pe_iv_now_trades(self):
        signal = self._make_signal(confidence="MEDIUM", direction=Direction.BEARISH)
        result = self._tick_with(signal, self._make_atm(ce_iv=10.0, pe_iv=15.0))
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
