"""
Tests for TASK-177 engine wiring: TrendContinuationDetector takes the 3rd
priority slot (behind breakout/oi_wall, ahead of exhaustion), is gated by
continuation_enabled (master switch, off on expiry) and continuation_alert_only
(phase-1 observation mode, same convention as exhaustion's Filter D), and its
signals are trend-aligned by construction so the trend filter (Filter E)
never touches them.
"""
import dataclasses
import unittest
from unittest.mock import MagicMock
from datetime import datetime

from config import settings
from config_profiles import NON_EXPIRY_CONFIG
from engine import AresEngine
from models import AresSignal, SetupType, Direction, ATMStrikes
from detectors.continuation import TrendContinuationDetector


def make_atm(ce_iv=12.0, pe_iv=12.0):
    atm = MagicMock(spec=ATMStrikes)
    atm.ce = MagicMock()
    atm.ce.iv = ce_iv
    atm.ce.oi = 100
    atm.ce.oi_prev = 100
    atm.pe = MagicMock()
    atm.pe.iv = pe_iv
    atm.pe.oi = 100
    atm.pe.oi_prev = 100
    atm.spot_price = 24115.0
    return atm


def make_candle(close=24115.0, vwap=24100.0):
    candle = MagicMock()
    candle.high = close + 1.0
    candle.low = close - 1.0
    candle.close = close
    candle.open = close - 5.0
    candle.volume = 100000
    candle.vwap = vwap
    return candle


def make_continuation_signal(confidence="HIGH", direction=Direction.BULLISH):
    # R:R 2.0, trend-aligned by construction: bullish entry above vwap in an
    # uptrend (pdl below both close and vwap).
    if direction == Direction.BULLISH:
        stop_loss, target_1, target_2, opt = 24075.0, 24165.0, 24215.0, "CE"
    else:
        stop_loss, target_1, target_2, opt = 24125.0, 24045.0, 23995.0, "PE"
    return AresSignal(
        setup_type=SetupType.TREND_CONTINUATION,
        direction=direction,
        trigger_price=24115.0,
        entry_zone=(24110.0, 24120.0),
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=target_2,
        confidence=confidence,
        reasons=["Trend continuation resumption"],
        timestamp=datetime.now(),
        strike_to_trade=24100,
        option_type=opt,
    )


class TestEngineWiring(unittest.TestCase):

    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)
        self.engine = AresEngine()

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def test_engine_has_continuation_detector(self):
        self.assertIsInstance(self.engine.continuation_detector, TrendContinuationDetector)

    def test_priority_breakout_shortcircuits_continuation(self):
        breakout_signal = MagicMock()
        breakout_signal.confidence = "HIGH"
        breakout_signal.setup_type = SetupType.FAILED_BREAKOUT
        breakout_signal.alert_only = False
        breakout_signal.trigger_price = 24000.0
        breakout_signal.stop_loss = 23950.0
        breakout_signal.target_1 = 24100.0
        breakout_signal.reasons = []

        self.engine.breakout_detector.update = MagicMock(return_value=breakout_signal)
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.continuation_detector.update = MagicMock(return_value=make_continuation_signal())
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)

        self.engine.tick(make_candle(), [], make_atm(), 0.0, [], 24200.0, 24000.0)
        self.engine.continuation_detector.update.assert_not_called()

    def test_continuation_disabled_never_reaches_engine_output(self):
        settings.apply_profile(dataclasses.replace(NON_EXPIRY_CONFIG, continuation_enabled=False))
        self.engine.breakout_detector.update = MagicMock(return_value=None)
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.continuation_detector.update = MagicMock(return_value=make_continuation_signal())
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)

        result = self.engine.tick(make_candle(), [], make_atm(), 0.0, [], 24200.0, 24000.0)
        self.assertIsNone(result)
        self.engine.continuation_detector.update.assert_not_called()

    def test_continuation_signal_is_alert_only_by_default(self):
        self.engine.breakout_detector.update = MagicMock(return_value=None)
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.continuation_detector.update = MagicMock(return_value=make_continuation_signal())
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)

        result = self.engine.tick(make_candle(), [], make_atm(), 0.0, [], 24200.0, 24000.0)
        self.assertIsNotNone(result)
        self.assertTrue(result.alert_only)
        self.assertTrue(any("continuation_alert_only" in r for r in result.reasons))

    def test_continuation_does_not_consume_cooldown_while_observation_only(self):
        self.engine.breakout_detector.update = MagicMock(return_value=None)
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.continuation_detector.update = MagicMock(return_value=make_continuation_signal())
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)

        self.engine.tick(make_candle(), [], make_atm(), 0.0, [], 24200.0, 24000.0)
        self.assertIsNone(self.engine.last_signal_time)

    def test_trend_filter_never_downgrades_aligned_continuation_signal(self):
        """Continuation signals are trend-aligned by construction (same
        VWAP/PDH-PDL rule as Filter E), so even with alert_only disabled
        (simulating phase-2 live), Filter E must never touch them."""
        settings.apply_profile(dataclasses.replace(NON_EXPIRY_CONFIG, continuation_alert_only=False))
        self.engine.breakout_detector.update = MagicMock(return_value=None)
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.continuation_detector.update = MagicMock(return_value=make_continuation_signal())
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)

        # close(24115) > vwap(24100) and > pdl(24000) -> uptrend; the
        # bullish continuation signal is aligned, not counter-trend.
        result = self.engine.tick(make_candle(close=24115.0, vwap=24100.0), [], make_atm(), 0.0, [],
                                   24200.0, 24000.0)
        self.assertIsNotNone(result)
        self.assertFalse(result.alert_only)
        self.assertFalse(any("trend_filter_enabled" in r for r in result.reasons))

    def test_expiry_profile_disables_continuation(self):
        from config_profiles import EXPIRY_CONFIG
        settings.apply_profile(EXPIRY_CONFIG)
        engine = AresEngine()
        engine.breakout_detector.update = MagicMock(return_value=None)
        engine.oi_wall_detector.update = MagicMock(return_value=None)
        engine.continuation_detector.update = MagicMock(return_value=make_continuation_signal())
        engine.exhaustion_detector.update = MagicMock(return_value=None)

        result = engine.tick(make_candle(), [], make_atm(), 0.0, [], 24200.0, 24000.0)
        self.assertIsNone(result)
        engine.continuation_detector.update.assert_not_called()
