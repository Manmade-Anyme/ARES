"""
Tests for TASK-188: the stateful OI wall detector must observe every candle.

TASK-169 made OIWallDetector stateful (candidate on candle N, confirmation on
candle N+1), but AresEngine.tick() skips it on two paths:

  * the cooldown check returns before any detector runs, so update() is not
    called at all for the 15 minutes after any signal;
  * the `or` short-circuit means a firing breakout detector skips it too.

While the detector was stateless (pre-TASK-169) a skipped candle was harmless.
Now it corrupts the state machine: a pending candidate survives the gap and is
confirmed against a candle many minutes later, which is both a missed signal
and a wrong signal.

Fix: always advance the detector; gate only signal *emission* on cooldown.
"""
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

from config import settings
from config_profiles import NON_EXPIRY_CONFIG
from engine import AresEngine
from models import ATMStrikes


class TestOIWallNotStarved(unittest.TestCase):

    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)
        self.engine = AresEngine()

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def _candle(self, close):
        c = MagicMock()
        c.high, c.low, c.close, c.open, c.volume = close + 1.0, close - 1.0, close, close, 100000
        c.timestamp = datetime.now()
        return c

    def _atm(self, spot=24000.0):
        atm = MagicMock(spec=ATMStrikes)
        atm.ce = MagicMock(); atm.ce.iv = 12.0; atm.ce.oi = 1000; atm.ce.oi_prev = 1000
        atm.pe = MagicMock(); atm.pe.iv = 12.0; atm.pe.oi = 1000; atm.pe.oi_prev = 1000
        atm.spot_price = spot
        return atm

    def _tick(self):
        return self.engine.tick(
            candle=self._candle(24000.0), full_chain=[], atm=self._atm(),
            iv_change_pct=0.0, levels=[],
        )

    def test_oi_wall_detector_advanced_during_cooldown(self):
        """Cooldown must not stop the detector's state machine from advancing."""
        self.engine.breakout_detector.update = MagicMock(return_value=None)
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.continuation_detector.update = MagicMock(return_value=None)
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)

        # Put the engine into cooldown.
        self.engine.last_signal_time = datetime.now() - timedelta(minutes=1)
        self.assertLess(1, settings.signal_cooldown_minutes)

        self._tick()
        self.engine.oi_wall_detector.update.assert_called_once()

    def test_oi_wall_detector_advanced_when_breakout_fires(self):
        """A firing higher-priority detector must not skip the OI wall update."""
        breakout_signal = MagicMock()
        breakout_signal.setup_type = MagicMock()
        breakout_signal.setup_type.value = "FAILED_BREAKOUT"
        # Real levels so the R:R gate can evaluate (2:1, comfortably passing).
        breakout_signal.trigger_price = 24000.0
        breakout_signal.stop_loss = 23990.0
        breakout_signal.target_1 = 24020.0
        breakout_signal.reasons = []
        self.engine.breakout_detector.update = MagicMock(return_value=breakout_signal)
        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.continuation_detector.update = MagicMock(return_value=None)
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)

        with patch('engine.apply_per_type_levels'):
            self._tick()
        self.engine.oi_wall_detector.update.assert_called_once()

    def test_cooldown_still_suppresses_emission(self):
        """Advancing the detector must not leak signals out during cooldown."""
        oi_signal = MagicMock()
        oi_signal.setup_type = MagicMock()
        oi_signal.setup_type.value = "OI_WALL_REJECTION"
        self.engine.breakout_detector.update = MagicMock(return_value=None)
        self.engine.oi_wall_detector.update = MagicMock(return_value=oi_signal)
        self.engine.continuation_detector.update = MagicMock(return_value=None)
        self.engine.exhaustion_detector.update = MagicMock(return_value=None)

        self.engine.last_signal_time = datetime.now() - timedelta(minutes=1)
        self.assertIsNone(self._tick(), "cooldown must still suppress the emitted signal")
        self.engine.oi_wall_detector.update.assert_called_once()


if __name__ == "__main__":
    unittest.main()
