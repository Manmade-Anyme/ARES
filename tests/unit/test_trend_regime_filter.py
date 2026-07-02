"""
Tests for TASK-173 audit P2 item 16: trend-regime filter.

Regime is derived from VWAP + PDH/PDL position:
  - Uptrend:   close > vwap AND close > pdl
  - Downtrend: close < vwap AND close < pdh
  - Otherwise: regime is ambiguous, no counter-trend action is taken.

Counter-trend HIGH confidence signals are downgraded to observation-only
(same convention as exhaustion's alert_only — visible, never traded).
Counter-trend MEDIUM confidence signals are suppressed outright, matching
the speed/IV-crush filters' MEDIUM-suppression convention.
"""
import unittest
from unittest.mock import MagicMock
from datetime import datetime

from config import settings
from config_profiles import TuningConfig, NON_EXPIRY_CONFIG
from engine import AresEngine
from models import AresSignal, SetupType, Direction, ATMStrikes


class TestTrendFilterConfigDefault(unittest.TestCase):
    def test_trend_filter_enabled_defaults_true(self):
        self.assertTrue(TuningConfig().trend_filter_enabled)


class TestTrendRegimeFilter(unittest.TestCase):

    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)
        self.engine = AresEngine()

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def _make_candle(self, close, vwap, volume=100000):
        candle = MagicMock()
        candle.high = close + 1.0
        candle.low = close - 1.0
        candle.close = close
        candle.open = close
        candle.volume = volume
        candle.vwap = vwap
        return candle

    def _make_atm(self, ce_iv=12.0, pe_iv=12.0):
        atm = MagicMock(spec=ATMStrikes)
        atm.ce = MagicMock()
        atm.ce.iv = ce_iv
        atm.pe = MagicMock()
        atm.pe.iv = pe_iv
        atm.spot_price = 24000.0
        return atm

    def _make_signal(self, confidence, direction, setup_type=SetupType.FAILED_BREAKOUT):
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

    def _tick_with(self, signal, candle, pdh=24100.0, pdl=24000.0):
        self.engine.breakout_detector.update = MagicMock(return_value=signal)
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)
        return self.engine.tick(candle, [], self._make_atm(), 0.0, [], pdh, pdl)

    # ── Counter-trend suppression / downgrade ───────────────────────────────

    def test_high_confidence_bullish_downgraded_in_downtrend(self):
        """close(23980) < vwap(24010) and < pdh(24100) -> downtrend. A bullish
        signal fighting it is downgraded to observation-only, not discarded."""
        signal = self._make_signal("HIGH", Direction.BULLISH)
        candle = self._make_candle(close=23980.0, vwap=24010.0)
        result = self._tick_with(signal, candle, pdh=24100.0, pdl=24000.0)
        self.assertIsNotNone(result)
        self.assertTrue(result.alert_only)
        self.assertIn("Counter-trend", result.reasons[-1])

    def test_medium_confidence_bullish_suppressed_in_downtrend(self):
        signal = self._make_signal("MEDIUM", Direction.BULLISH)
        candle = self._make_candle(close=23980.0, vwap=24010.0)
        result = self._tick_with(signal, candle, pdh=24100.0, pdl=24000.0)
        self.assertIsNone(result)

    def test_high_confidence_bearish_downgraded_in_uptrend(self):
        """close(24120) > vwap(24090) and > pdl(24000) -> uptrend. A bearish
        signal fighting it is downgraded to observation-only."""
        signal = self._make_signal("HIGH", Direction.BEARISH)
        candle = self._make_candle(close=24120.0, vwap=24090.0)
        result = self._tick_with(signal, candle, pdh=24200.0, pdl=24000.0)
        self.assertIsNotNone(result)
        self.assertTrue(result.alert_only)

    def test_medium_confidence_bearish_suppressed_in_uptrend(self):
        signal = self._make_signal("MEDIUM", Direction.BEARISH)
        candle = self._make_candle(close=24120.0, vwap=24090.0)
        result = self._tick_with(signal, candle, pdh=24200.0, pdl=24000.0)
        self.assertIsNone(result)

    # ── Aligned (with-trend) signals pass through untouched ─────────────────

    def test_bullish_signal_passes_in_uptrend(self):
        signal = self._make_signal("MEDIUM", Direction.BULLISH)
        candle = self._make_candle(close=24120.0, vwap=24090.0)
        result = self._tick_with(signal, candle, pdh=24200.0, pdl=24000.0)
        self.assertIsNotNone(result)
        self.assertFalse(result.alert_only)

    def test_bearish_signal_passes_in_downtrend(self):
        signal = self._make_signal("MEDIUM", Direction.BEARISH)
        candle = self._make_candle(close=23980.0, vwap=24010.0)
        result = self._tick_with(signal, candle, pdh=24100.0, pdl=24000.0)
        self.assertIsNotNone(result)
        self.assertFalse(result.alert_only)

    # ── Ambiguous regime: no action either direction ────────────────────────

    def test_ambiguous_regime_no_suppression(self):
        """close(24050) > vwap(24010) [uptrend leg holds] but close < pdl(24060)
        so the uptrend condition fails, and close is not < vwap so the
        downtrend condition fails too -> ambiguous regime, nothing suppressed."""
        signal = self._make_signal("MEDIUM", Direction.BULLISH)
        candle = self._make_candle(close=24050.0, vwap=24010.0)
        result = self._tick_with(signal, candle, pdh=24100.0, pdl=24060.0)
        self.assertIsNotNone(result)
        self.assertFalse(result.alert_only)

    # ── Config off-switch ────────────────────────────────────────────────────

    def test_disabled_flag_lets_counter_trend_high_through_unflagged(self):
        settings.apply_profile(TuningConfig(trend_filter_enabled=False))
        engine = AresEngine()
        engine.breakout_detector.update = MagicMock(
            return_value=self._make_signal("HIGH", Direction.BULLISH)
        )
        engine.oi_wall_detector.update = MagicMock(return_value=None)
        engine.exhaustion_detector.update = MagicMock(return_value=None)
        candle = self._make_candle(close=23980.0, vwap=24010.0)
        result = engine.tick(candle, [], self._make_atm(), 0.0, [], 24100.0, 24000.0)
        self.assertIsNotNone(result)
        self.assertFalse(result.alert_only)

    # ── Backward compatibility: pdh/pdl omitted (existing call sites) ───────

    def test_omitted_pdh_pdl_defaults_do_not_crash_or_suppress(self):
        signal = self._make_signal("HIGH", Direction.BULLISH)
        candle = self._make_candle(close=23980.0, vwap=24010.0)
        self.engine.breakout_detector.update = MagicMock(return_value=signal)
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)
        result = self.engine.tick(candle, [], self._make_atm(), 0.0, [])
        self.assertIsNotNone(result)
        self.assertFalse(result.alert_only)

    def test_already_alert_only_signal_skips_trend_check(self):
        """Exhaustion's alert_only is already set upstream (Filter D) — the
        trend filter shouldn't need to re-evaluate an already-gated signal."""
        signal = self._make_signal("HIGH", Direction.BULLISH, setup_type=SetupType.EXHAUSTION_REVERSAL)
        settings.apply_profile(NON_EXPIRY_CONFIG)
        candle = self._make_candle(close=23980.0, vwap=24010.0)
        result = self._tick_with(signal, candle, pdh=24100.0, pdl=24000.0)
        self.assertIsNotNone(result)
        self.assertTrue(result.alert_only)
        # Only Filter D's reason was added, trend filter added nothing extra
        self.assertEqual(
            sum(1 for r in result.reasons if "Counter-trend" in r), 0
        )


if __name__ == "__main__":
    unittest.main()
