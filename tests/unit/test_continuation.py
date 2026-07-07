"""
Tests for TASK-177: TrendContinuationDetector.

State machine: regime persistence (same VWAP+PDH/PDL rule as engine Filter
E) -> armed -> pullback (toward VWAP or a structural level) -> resumption
candle -> scored signal. SL is the exact pullback extreme (TASK-175
no-buffer convention); targets reuse the shared structural target selection.
"""
import dataclasses
import unittest
from datetime import datetime, timedelta

from config import settings
from config_profiles import TuningConfig, NON_EXPIRY_CONFIG, EXPIRY_CONFIG
from models import OHLCVCandle, ResistanceLevel, SetupType, Direction
from detectors.continuation import TrendContinuationDetector


def candle(close, vwap, open_=None, high=None, low=None, volume=100000, minute=0):
    return OHLCVCandle(
        timestamp=datetime(2026, 7, 7, 9, 15) + timedelta(minutes=minute),
        open=open_ if open_ is not None else close,
        high=high if high is not None else max(close, open_ if open_ is not None else close) + 0.5,
        low=low if low is not None else min(close, open_ if open_ is not None else close) - 0.5,
        close=close,
        volume=volume,
        vwap=vwap,
    )


class TestConfigFields(unittest.TestCase):
    def test_defaults_present(self):
        cfg = TuningConfig()
        self.assertTrue(cfg.continuation_enabled)
        self.assertEqual(cfg.continuation_regime_min_candles, 15)
        self.assertEqual(cfg.continuation_pullback_vwap_pts, 10.0)
        self.assertEqual(cfg.continuation_pullback_max_candles, 10)
        self.assertEqual(cfg.continuation_resume_volume_ratio, 1.2)
        self.assertEqual(cfg.continuation_min_score, 2)

    def test_expiry_enables_live_continuation(self):
        """Continuation runs on expiry too, using the faster expiry-specific
        knobs (shorter regime/pullback windows, higher score bar) reserved
        since TASK-177."""
        self.assertTrue(EXPIRY_CONFIG.continuation_enabled)


class TestContinuationStateMachine(unittest.TestCase):

    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)
        self.det = TrendContinuationDetector()
        self.pdh, self.pdl = 24200.0, 24000.0

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def _run_regime(self, n, close=24150.0, vwap=24100.0, start=0):
        """Feed n consecutive aligned (uptrend) candles."""
        sig = None
        for i in range(n):
            sig = self.det.update(
                candle(close, vwap, minute=start + i), avg_volume=100000, levels=[],
                pdh=self.pdh, pdl=self.pdl,
            )
        return sig

    def test_noop_without_pdh_pdl(self):
        result = self.det.update(candle(24110.0, 24100.0), avg_volume=100000, levels=[])
        self.assertIsNone(result)
        self.assertIsNone(self.det.state)

    def test_arms_after_min_regime_candles(self):
        self._run_regime(settings.continuation_regime_min_candles - 1)
        self.assertFalse(self.det.state.armed)
        self._run_regime(1, start=settings.continuation_regime_min_candles - 1)
        self.assertTrue(self.det.state.armed)

    def test_opposite_regime_resets_before_arming(self):
        self._run_regime(5)
        self.assertEqual(self.det.state.regime_candles, 5)
        # Hard downtrend candle: close < vwap and close < pdh
        self.det.update(candle(23990.0, 24050.0, minute=5), avg_volume=100000, levels=[],
                         pdh=self.pdh, pdl=self.pdl)
        self.assertEqual(self.det.state.direction, Direction.BEARISH)
        self.assertEqual(self.det.state.regime_candles, 1)

    def test_full_bullish_continuation_emits_signal(self):
        n = settings.continuation_regime_min_candles
        self._run_regime(n)
        self.assertTrue(self.det.state.armed)

        # Pullback candle: close within pullback band of VWAP, low sets the extreme
        pullback = candle(24098.0, 24100.0, open_=24105.0, high=24106.0, low=24096.0, minute=n)
        result = self.det.update(pullback, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertIsNone(result)
        self.assertTrue(self.det.state.in_pullback)
        self.assertEqual(self.det.state.pullback_extreme, 24096.0)

        # First resumption candle only arms the confirmation gate (TASK-179:
        # a single up-close inside an ongoing pullback isn't proof the
        # decline has actually stalled) -- no signal yet.
        first = candle(24110.0, 24100.0, open_=24098.0, volume=150000, minute=n + 1)
        result = self.det.update(first, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertIsNone(result)
        self.assertTrue(self.det.state.resume_pending)

        # Second consecutive resumption candle confirms -> signal fires here.
        levels = [ResistanceLevel(price=24300.0, source="OI_WALL", strength=2)]
        resume = candle(24115.0, 24100.0, open_=24108.0, volume=150000, minute=n + 2)
        signal = self.det.update(resume, avg_volume=100000, levels=levels, pdh=self.pdh, pdl=self.pdl)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.setup_type, SetupType.TREND_CONTINUATION)
        self.assertEqual(signal.direction, Direction.BULLISH)
        self.assertEqual(signal.option_type, "CE")
        self.assertEqual(signal.trigger_price, 24115.0)  # entry is the 2nd confirming candle's close
        self.assertEqual(signal.stop_loss, 24096.0)  # exact pullback extreme, no buffer
        self.assertLess(signal.target_1, signal.target_2)
        self.assertIsNone(self.det.state)  # reset after resolving

    def test_single_resumption_candle_does_not_fire(self):
        """Regression guard for the exact failure this gate fixes: a lone
        up-close candle inside a still-declining pullback (2026-07-06 13:57
        NIFTY case) must not resolve into a signal on its own."""
        n = settings.continuation_regime_min_candles
        self._run_regime(n)
        pullback = candle(24098.0, 24100.0, open_=24105.0, low=24096.0, minute=n)
        self.det.update(pullback, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)

        resume = candle(24115.0, 24100.0, open_=24098.0, volume=150000, minute=n + 1)
        signal = self.det.update(resume, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertIsNone(signal)
        self.assertIsNotNone(self.det.state)
        self.assertTrue(self.det.state.in_pullback)

    def test_failed_second_candle_resets_pending_not_state(self):
        """A confirming candle followed by a non-confirming one doesn't
        blow up the whole candidate -- it just clears the pending flag and
        keeps tracking the pullback, so a later genuine 2-candle confirm
        can still fire."""
        n = settings.continuation_regime_min_candles
        self._run_regime(n)
        pullback = candle(24098.0, 24100.0, open_=24105.0, low=24096.0, minute=n)
        self.det.update(pullback, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)

        first = candle(24110.0, 24100.0, open_=24098.0, volume=150000, minute=n + 1)
        self.det.update(first, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertTrue(self.det.state.resume_pending)

        # Fails to confirm: closes back down, below its own open.
        fails = candle(24097.0, 24100.0, open_=24109.0, low=24096.5, minute=n + 2)
        result = self.det.update(fails, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertIsNone(result)
        self.assertFalse(self.det.state.resume_pending)
        self.assertTrue(self.det.state.in_pullback)

        first_again = candle(24112.0, 24100.0, open_=24099.0, volume=150000, minute=n + 3)
        self.det.update(first_again, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertTrue(self.det.state.resume_pending)
        confirm = candle(24118.0, 24100.0, open_=24113.0, volume=150000, minute=n + 4)
        signal = self.det.update(confirm, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertIsNotNone(signal)

    def test_full_bearish_continuation_emits_signal(self):
        pdh, pdl = 24200.0, 23900.0
        n = settings.continuation_regime_min_candles
        sig = None
        for i in range(n):
            sig = self.det.update(candle(24010.0, 24050.0, minute=i), avg_volume=100000, levels=[],
                                   pdh=pdh, pdl=pdl)
        self.assertTrue(self.det.state.armed)
        self.assertEqual(self.det.state.direction, Direction.BEARISH)

        pullback = candle(24055.0, 24050.0, open_=24045.0, high=24060.0, low=24044.0, minute=n)
        result = self.det.update(pullback, avg_volume=100000, levels=[], pdh=pdh, pdl=pdl)
        self.assertIsNone(result)
        self.assertEqual(self.det.state.pullback_extreme, 24060.0)

        first = candle(24040.0, 24050.0, open_=24048.0, volume=150000, minute=n + 1)
        result = self.det.update(first, avg_volume=100000, levels=[], pdh=pdh, pdl=pdl)
        self.assertIsNone(result)
        self.assertTrue(self.det.state.resume_pending)

        levels = [ResistanceLevel(price=23800.0, source="PDL", strength=2)]
        resume = candle(24030.0, 24050.0, open_=24038.0, volume=150000, minute=n + 2)
        signal = self.det.update(resume, avg_volume=100000, levels=levels, pdh=pdh, pdl=pdl)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, Direction.BEARISH)
        self.assertEqual(signal.option_type, "PE")
        self.assertEqual(signal.stop_loss, 24060.0)
        self.assertGreater(signal.target_1, signal.target_2)

    def test_pullback_timeout_resets_state(self):
        n = settings.continuation_regime_min_candles
        self._run_regime(n)
        pullback = candle(24098.0, 24100.0, open_=24105.0, low=24096.0, minute=n)
        self.det.update(pullback, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertTrue(self.det.state.in_pullback)

        # Feed flat (tied-to-vwap, non-resuming) candles past the pullback
        # window. Close pinned exactly to vwap so neither the uptrend nor
        # downtrend regime rule fires (avoids exercising the hard-break path).
        for i in range(settings.continuation_pullback_max_candles + 1):
            flat = candle(24100.0, 24100.0, open_=24100.0, minute=n + 1 + i)
            result = self.det.update(flat, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
            self.assertIsNone(result)
        self.assertIsNone(self.det.state)

    def test_hard_break_during_pullback_aborts(self):
        """A genuine breach of the prior day's low (not just a vwap dip)
        ends the candidate outright — the looser pullback tolerance only
        covers noise around VWAP, not a structural failure."""
        n = settings.continuation_regime_min_candles
        self._run_regime(n)
        pullback = candle(24098.0, 24100.0, open_=24105.0, low=24096.0, minute=n)
        self.det.update(pullback, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertTrue(self.det.state.in_pullback)

        # close (23980) < pdl (24000) -> structural breach of the BULLISH regime
        breach = candle(23980.0, 24050.0, minute=n + 1)
        result = self.det.update(breach, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertIsNone(result)
        self.assertIsNone(self.det.state)

    def test_vwap_dip_during_pullback_does_not_abort(self):
        """The looser armed-phase rule: dipping below VWAP (but not
        breaching PDL) is the pullback itself, not a regime break."""
        n = settings.continuation_regime_min_candles
        self._run_regime(n)
        pullback = candle(24098.0, 24100.0, open_=24105.0, low=24096.0, minute=n)
        self.det.update(pullback, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertTrue(self.det.state.in_pullback)

        # Still below vwap, still well above pdl (24000) -> tolerated, not a break
        deeper = candle(24095.0, 24100.0, open_=24097.0, low=24093.0, minute=n + 1)
        result = self.det.update(deeper, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)
        self.assertIsNone(result)
        self.assertIsNotNone(self.det.state)
        self.assertTrue(self.det.state.in_pullback)
        self.assertEqual(self.det.state.pullback_extreme, 24093.0)

    def test_low_score_resumption_suppressed(self):
        # dataclasses.replace + apply_profile, not direct attribute
        # assignment: Settings only overrides __getattr__, so a bare
        # `settings.x = y` creates a permanent shadow attribute on the
        # singleton that survives later apply_profile() calls in other tests.
        settings.apply_profile(dataclasses.replace(NON_EXPIRY_CONFIG, continuation_min_score=4))
        n = settings.continuation_regime_min_candles
        self._run_regime(n)
        pullback = candle(24098.0, 24100.0, open_=24105.0, low=24096.0, minute=n)
        self.det.update(pullback, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)

        first = candle(24110.0, 24100.0, open_=24098.0, volume=50000, minute=n + 1)
        self.det.update(first, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)

        # Nearby opposing resistance -> fails "room to run"; weak volume -> fails "resume volume"
        levels = [ResistanceLevel(price=24120.0, source="OI_WALL", strength=1)]
        resume = candle(24115.0, 24100.0, open_=24108.0, volume=50000, minute=n + 2)
        signal = self.det.update(resume, avg_volume=100000, levels=levels, pdh=self.pdh, pdl=self.pdl)
        self.assertIsNone(signal)
        self.assertIsNone(self.det.state)  # still resets after resolving

    def test_confidence_high_when_all_conditions_met(self):
        n = settings.continuation_regime_min_candles
        # Persist the regime to 2x the arming minimum *before* pulling back,
        # so "strong regime" is satisfied without the pullback window (which
        # is capped much shorter) having to carry that weight.
        self._run_regime(2 * n)
        self.assertGreaterEqual(self.det.state.regime_candles, 2 * n)

        pullback = candle(24098.0, 24100.0, open_=24105.0, low=24096.0, minute=2 * n)
        self.det.update(pullback, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)

        first = candle(24110.0, 24100.0, open_=24098.0, volume=200000, minute=2 * n + 1)
        self.det.update(first, avg_volume=100000, levels=[], pdh=self.pdh, pdl=self.pdl)

        levels = [ResistanceLevel(price=24500.0, source="OI_WALL", strength=2)]
        resume = candle(24115.0, 24100.0, open_=24108.0, volume=200000, minute=2 * n + 2)
        signal = self.det.update(resume, avg_volume=100000, levels=levels, pdh=self.pdh, pdl=self.pdl)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.confidence, "HIGH")
