"""
Tests for TASK-188: restore OI Wall Rejection signal flow.

TASK-169 added two gates to the OI wall candidate rule: a >=40% wick-rejection
requirement and a next-candle confirmation. Replaying the 9 closed OI-wall
trades against real 1-min NIFTY candles showed the two gates behave very
differently:

  * The wick gate blocked all four T2 winners (wicks 19.0%, 24.4%, 37.1%,
    21.6% — every one under the 0.4 threshold) and admitted only two trades,
    both losers. It is anti-predictive on this book and is dropped here.
  * The confirmation gate kept 3 of the 4 winners and removed a net -49.3 pts
    of losing trades. It is kept unchanged.

The wick ratio survives as a *confidence score* contributor
(_evaluate_confidence); it is only removed as a hard candidate gate.

Wick percentages below are the real candle geometry from the replayed trades.
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from detectors.oi_wall import OIWallDetector
from detectors.oi_wall_entry import OIWallEntryFilter
from models import OHLCVCandle


def _settings(mock_settings):
    mock_settings.oi_wall_min_oi = 4000000
    mock_settings.oi_wall_min_oi_change_pct = 5.0
    mock_settings.oi_wall_approach_distance = 80.0
    mock_settings.oi_wall_test_distance = 20.0
    mock_settings.oi_wall_conviction_multiplier = 1.5
    mock_settings.oi_wall_wick_min_range_pts = 2.0
    mock_settings.oi_wall_wick_rejection_ratio = 0.4
    mock_settings.structural_target_min_distance_pts = 20.0
    mock_settings.strike_interval = 50
    mock_settings.entry_zone_offset_pts = 5.0


CE_WALL_CHAIN = [{
    "strike": 24100,
    "ce_oi": 5000000,
    "ce_oi_prev": 4500000,
    "ce_oi_change_pct": 11.1,
    "pe_oi": 100000,
    "pe_oi_prev": 100000,
    "pe_oi_change_pct": 0.0,
}]

PE_WALL_CHAIN = [{
    "strike": 24000,
    "ce_oi": 100000,
    "ce_oi_prev": 100000,
    "ce_oi_change_pct": 0.0,
    "pe_oi": 5000000,
    "pe_oi_prev": 4500000,
    "pe_oi_change_pct": 11.1,
}]


class TestWickGateDropped(unittest.TestCase):
    """A shallow-wick touch must still become a candidate (TASK-188)."""

    def setUp(self):
        self.detector = OIWallDetector()
        self.filter = OIWallEntryFilter()

    @patch('detectors.oi_wall.settings')
    def test_shallow_upper_wick_still_becomes_ce_candidate(self, mock_settings):
        _settings(mock_settings)
        # Bearish touch of the 24100 CE wall. Range 18, upper wick 6 -> 33%,
        # under the old 0.4 gate. TASK-169 dropped this; it must now register.
        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24075.0, high=24081.0, low=24063.0, close=24065.0, volume=1000,
        )
        bias = self.detector.update(spot=24080.0, full_chain=CE_WALL_CHAIN, candle=candle, levels=[])
        self.assertIsNotNone(bias)
        decision = self.filter.update(bias=bias, candle=candle, levels=[])
        self.assertEqual(self.filter.state, "INTERACTED")

    @patch('detectors.oi_wall.settings')
    def test_shallow_lower_wick_still_becomes_pe_candidate(self, mock_settings):
        _settings(mock_settings)
        # Bullish bounce off the 24000 PE wall with a shallow lower wick.
        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24025.0, high=24043.0, low=24019.0, close=24035.0, volume=1000,
        )
        bias = self.detector.update(spot=24020.0, full_chain=PE_WALL_CHAIN, candle=candle, levels=[])
        self.assertIsNotNone(bias)
        decision = self.filter.update(bias=bias, candle=candle, levels=[])
        self.assertEqual(self.filter.state, "INTERACTED")

    @patch('detectors.oi_wall.settings')
    def test_zero_wick_touch_still_becomes_candidate(self, mock_settings):
        _settings(mock_settings)
        # The 2026-06-29 12:32 trade had a 0.0% wick. Geometry alone must not
        # veto the candidate — only the wall/approach/test/direction rules do.
        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24085.0, high=24085.0, low=24065.0, close=24065.0, volume=1000,
        )
        bias = self.detector.update(spot=24080.0, full_chain=CE_WALL_CHAIN, candle=candle, levels=[])
        self.assertIsNotNone(bias)
        decision = self.filter.update(bias=bias, candle=candle, levels=[])
        self.assertEqual(self.filter.state, "INTERACTED")


class TestConfirmationKept(unittest.TestCase):
    """The next-candle confirmation is retained unchanged (TASK-188)."""

    def setUp(self):
        self.detector = OIWallDetector()
        self.filter = OIWallEntryFilter()

    @patch('detectors.oi_wall_entry.settings')
    @patch('detectors.oi_wall.settings')
    def test_shallow_wick_candidate_fires_only_after_confirmation(self, mock_oi_settings, mock_entry_settings):
        _settings(mock_oi_settings)
        _settings(mock_entry_settings)
        mock_entry_settings.oi_wall_persistence_snapshots = 1
        mock_entry_settings.oi_wall_min_excursion_pts = 10.0
        mock_entry_settings.oi_wall_retest_distance_pts = 20.0
        t0 = datetime.now()
        candle1 = OHLCVCandle(
            timestamp=t0,
            open=24075.0, high=24081.0, low=24063.0, close=24065.0, volume=1000,
        )
        bias1 = self.detector.update(spot=24080.0, full_chain=CE_WALL_CHAIN, candle=candle1, levels=[])
        decision1 = self.filter.update(bias=bias1, candle=candle1, levels=[])
        self.assertEqual(self.filter.state, "INTERACTED")

        # Excursion candle (low 24050 is 50 pts away from 24100 -> RETEST_READY)
        candle2 = OHLCVCandle(
            timestamp=t0 + timedelta(minutes=1),
            open=24064.0, high=24066.0, low=24050.0, close=24055.0, volume=1000,
        )
        bias2 = self.detector.update(spot=24055.0, full_chain=CE_WALL_CHAIN, candle=candle2, levels=[])
        decision2 = self.filter.update(bias=bias2, candle=candle2, levels=[])
        self.assertEqual(self.filter.state, "RETEST_READY")

        # Re-test candle tests wall and defends: high 24085 (within 20 pts of 24100), close 24075 (<= 24100)
        candle3 = OHLCVCandle(
            timestamp=t0 + timedelta(minutes=2),
            open=24060.0, high=24085.0, low=24058.0, close=24075.0, volume=1000,
        )
        bias3 = self.detector.update(spot=24075.0, full_chain=CE_WALL_CHAIN, candle=candle3, levels=[])
        decision3 = self.filter.update(bias=bias3, candle=candle3, levels=[])
        self.assertEqual(decision3.status, "WAITING")
        candle4 = OHLCVCandle(
            timestamp=t0 + timedelta(minutes=3),
            open=24075.0, high=24076.0, low=24050.0, close=24055.0, volume=1000,
        )
        bias4 = self.detector.update(spot=24055.0, full_chain=CE_WALL_CHAIN, candle=candle4, levels=[])
        decision4 = self.filter.update(bias=bias4, candle=candle4, levels=[])
        self.assertEqual(decision4.status, "QUALIFIED")
        signal = self.detector.build_signal(decision4, candle4, spot=24055.0, levels=[])
        self.assertIsNotNone(signal, "confirmed shallow-wick candidate must fire")
        self.assertEqual(signal.option_type, "PE")

    @patch('detectors.oi_wall_entry.settings')
    @patch('detectors.oi_wall.settings')
    def test_unconfirmed_shallow_wick_candidate_does_not_fire(self, mock_oi_settings, mock_entry_settings):
        _settings(mock_oi_settings)
        _settings(mock_entry_settings)
        mock_entry_settings.oi_wall_persistence_snapshots = 1
        mock_entry_settings.oi_wall_min_excursion_pts = 10.0
        mock_entry_settings.oi_wall_retest_distance_pts = 20.0
        t0 = datetime.now()
        candle1 = OHLCVCandle(
            timestamp=t0,
            open=24075.0, high=24081.0, low=24063.0, close=24065.0, volume=1000,
        )
        bias1 = self.detector.update(spot=24080.0, full_chain=CE_WALL_CHAIN, candle=candle1, levels=[])
        decision1 = self.filter.update(bias=bias1, candle=candle1, levels=[])
        self.assertEqual(self.filter.state, "INTERACTED")

        # Re-test candle with spot still below wall (24095), but close reaches 24105 > strike 24100 -> breach -> EXPIRED
        candle2 = OHLCVCandle(
            timestamp=t0 + timedelta(minutes=1),
            open=24090.0, high=24110.0, low=24080.0, close=24105.0, volume=1000,
        )
        bias2 = self.detector.update(spot=24095.0, full_chain=CE_WALL_CHAIN, candle=candle2, levels=[])
        decision2 = self.filter.update(bias=bias2, candle=candle2, levels=[])
        self.assertEqual(decision2.status, "EXPIRED")


class TestWritersHoldingRemoved(unittest.TestCase):
    """writers_holding was dead code: the wall-selection gate already requires
    ce_oi_change_pct > 5%, which implies ce_oi > ce_oi_prev. Its removal must
    not change behaviour for any physically-real chain row."""

    @patch('detectors.oi_wall.settings')
    def test_candidate_forms_without_writers_holding_check(self, mock_settings):
        _settings(mock_settings)
        detector = OIWallDetector()
        entry_filter = OIWallEntryFilter()
        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24075.0, high=24081.0, low=24063.0, close=24065.0, volume=1000,
        )
        bias = detector.update(spot=24080.0, full_chain=CE_WALL_CHAIN, candle=candle, levels=[])
        decision = entry_filter.update(bias=bias, candle=candle, levels=[])
        self.assertEqual(entry_filter.state, "INTERACTED")

    @patch('detectors.oi_wall.settings')
    def test_defended_reason_not_asserted_unconditionally(self, mock_settings):
        _settings(mock_settings)
        detector = OIWallDetector()
        from models import Direction
        wall = dict(CE_WALL_CHAIN[0])
        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24095.0, high=24105.0, low=24090.0, close=24092.0, volume=1000,
        )
        signal = detector._build_signal(
            candle=candle, spot=24090.0, wall=wall,
            direction=Direction.BEARISH, option_type="PE", levels=[],
        )
        # The old code hardcoded "Option writers defended the level (OI did not
        # drop)" into every signal without ever checking it.
        self.assertFalse(
            any("did not drop" in r for r in signal.reasons),
            "unchecked 'writers defended' claim must not be asserted in the alert",
        )


if __name__ == "__main__":
    unittest.main()
