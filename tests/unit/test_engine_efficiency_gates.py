"""
Tests for TASK-171 audit P0 efficiency gates in AresEngine:
- R:R gate: reject signals whose risk (entry→SL) exceeds reward (entry→T1).
- Exhaustion alert-only: exhaustion signals are tagged observation-only.
- clear_cooldown(): allows immediate re-entry after a stop-out.
"""
import dataclasses
import unittest
from unittest.mock import MagicMock
from datetime import datetime, timedelta

from config import settings
from config_profiles import NON_EXPIRY_CONFIG
from engine import AresEngine
from models import ATMStrikes, AresSignal, SetupType, Direction


class TestEngineEfficiencyGates(unittest.TestCase):

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

    def _make_atm(self, spot=24000.0, iv=12.0):
        atm = MagicMock(spec=ATMStrikes)
        atm.ce = MagicMock()
        atm.ce.iv = iv
        atm.pe = MagicMock()
        atm.pe.iv = iv
        atm.spot_price = spot
        return atm

    def _make_signal(self, setup_type=SetupType.OI_WALL_REJECTION,
                     direction=Direction.BULLISH, trigger=24000.0,
                     stop_loss=23975.0, target_1=24035.0, confidence="HIGH"):
        opt_type = "CE" if direction == Direction.BULLISH else "PE"
        return AresSignal(
            setup_type=setup_type,
            direction=direction,
            trigger_price=trigger,
            entry_zone=(trigger - 5.0, trigger + 5.0),
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_1 + (35.0 if direction == Direction.BULLISH else -35.0),
            confidence=confidence,
            reasons=["Reason 1"],
            timestamp=datetime.now(),
            strike_to_trade=24000,
            option_type=opt_type
        )

    def _trending_buffer(self):
        """Fill the candle buffer so the speed filter never interferes."""
        for i in range(15):
            self.engine.candle_buffer.append(self._make_candle(24000.0 + i * 3))

    def _tick_with(self, signal, direction=Direction.BULLISH):
        self._trending_buffer()
        self.engine.breakout_detector.update = MagicMock(return_value=signal)
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)
        # Bearish so the anti-IV-crush bullish filter never interferes,
        # unless the test explicitly wants a bullish signal (IV kept low).
        return self.engine.tick(self._make_candle(24045.0), [], self._make_atm(), 0.0, [])

    # ── R:R gate ────────────────────────────────────────────────────────────

    def test_rr_gate_rejects_risk_greater_than_reward(self):
        # Risk 50 pts (SL 23950), reward 35 pts (T1 24035) → R:R 0.7 → reject
        signal = self._make_signal(stop_loss=23950.0, target_1=24035.0)
        result = self._tick_with(signal)
        self.assertIsNone(result)

    def test_rr_gate_allows_reward_at_least_equal_to_risk(self):
        # Risk 25 pts, reward 35 pts → R:R 1.4 → pass
        signal = self._make_signal(stop_loss=23975.0, target_1=24035.0)
        result = self._tick_with(signal)
        self.assertIsNotNone(result)

    def test_rr_gate_rejects_bearish_inverted_rr(self):
        # Bearish: entry 24000, SL 24046 (risk 46), T1 23965 (reward 35) → reject
        signal = self._make_signal(direction=Direction.BEARISH,
                                   stop_loss=24046.0, target_1=23965.0)
        result = self._tick_with(signal)
        self.assertIsNone(result)

    def test_rr_gate_rejects_zero_or_negative_risk(self):
        # SL on the wrong side of entry (degenerate) → reject, never divide by zero
        signal = self._make_signal(stop_loss=24000.0, target_1=24035.0)
        result = self._tick_with(signal)
        self.assertIsNone(result)

    # ── Exhaustion alert-only ───────────────────────────────────────────────

    def test_exhaustion_signal_is_tagged_alert_only_when_configured(self):
        """The exhaustion_alert_only gate mechanism (TASK-172) is unchanged
        by TASK-180's default flip to live -- force it on to test the
        mechanism directly rather than depend on the production default."""
        settings.apply_profile(dataclasses.replace(NON_EXPIRY_CONFIG, exhaustion_alert_only=True))
        signal = self._make_signal(setup_type=SetupType.EXHAUSTION_REVERSAL,
                                   direction=Direction.BEARISH,
                                   stop_loss=24020.0, target_1=23965.0)
        result = self._tick_with(signal)
        self.assertIsNotNone(result)
        self.assertTrue(result.alert_only)

    def test_non_exhaustion_signal_is_not_alert_only(self):
        signal = self._make_signal(stop_loss=23975.0, target_1=24035.0)
        result = self._tick_with(signal)
        self.assertIsNotNone(result)
        self.assertFalse(result.alert_only)

    def test_exhaustion_signal_is_live_by_default(self):
        """TASK-180: exhaustion_alert_only defaults to False now (both
        profiles) -- exhaustion signals are live/tradeable out of the box."""
        signal = self._make_signal(setup_type=SetupType.EXHAUSTION_REVERSAL,
                                   direction=Direction.BEARISH,
                                   stop_loss=24020.0, target_1=23965.0)
        result = self._tick_with(signal)
        self.assertIsNotNone(result)
        self.assertFalse(result.alert_only)

    # ── Cooldown reset after stop-out ───────────────────────────────────────

    def test_clear_cooldown_allows_immediate_next_signal(self):
        # Simulate a signal fired 1 minute ago (cooldown active)
        self.engine.last_signal_time = datetime.now() - timedelta(minutes=1)
        signal = self._make_signal(stop_loss=23975.0, target_1=24035.0)
        result = self._tick_with(signal)
        self.assertIsNone(result)  # blocked by cooldown

        self.engine.clear_cooldown()
        result = self._tick_with(signal)
        self.assertIsNotNone(result)  # cooldown cleared → passes


if __name__ == "__main__":
    unittest.main()
