import unittest
from unittest.mock import patch
from models import OHLCVCandle, Direction
from datetime import datetime
from detectors.oi_wall import OIWallDetector

class TestOIWallDetector(unittest.TestCase):
    def setUp(self):
        self.detector = OIWallDetector()

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_confidence_high(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_conviction_multiplier = 1.5
        mock_settings.oi_wall_wick_min_range_pts = 2.0
        mock_settings.structural_target_min_distance_pts = 20.0
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0

        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24095.0,
            high=24105.0,
            low=24090.0,
            close=24092.0,
            volume=1000
        )
        wall = {
            "strike": 24100,
            "ce_oi": 6000000,
            "ce_oi_prev": 5900000,
            "ce_oi_change_pct": 2.0,
            "pe_oi": 1000000,
            "pe_oi_prev": 1000000,
            "pe_oi_change_pct": 0.0
        }

        signal = self.detector._build_signal(
            candle=candle,
            spot=24090.0,
            wall=wall,
            direction=Direction.BEARISH,
            option_type="PE",
            levels=[]
        )
        self.assertEqual(signal.confidence, "HIGH")
        self.assertTrue(any("Massive wall size" in r for r in signal.reasons))
        self.assertTrue(any("tested wall deeply" in r for r in signal.reasons))
        self.assertFalse(any("Aggressive active defending" in r for r in signal.reasons))

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_confidence_medium(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_conviction_multiplier = 1.5
        mock_settings.oi_wall_wick_min_range_pts = 2.0
        mock_settings.structural_target_min_distance_pts = 20.0
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0

        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24095.0,
            high=24099.0,  # Below strike 24100 -> pierced is False
            low=24094.0,
            close=24095.0,
            volume=1000
        )
        wall = {
            "strike": 24100,
            "ce_oi": 4100000,
            "ce_oi_prev": 4000000,
            "ce_oi_change_pct": 2.5,
            "pe_oi": 1000000,
            "pe_oi_prev": 1000000,
            "pe_oi_change_pct": 0.0
        }

        signal = self.detector._build_signal(
            candle=candle,
            spot=24090.0,
            wall=wall,
            direction=Direction.BEARISH,
            option_type="PE",
            levels=[]
        )
        self.assertEqual(signal.confidence, "MEDIUM")
        self.assertFalse(any("tested wall deeply" in r for r in signal.reasons))
        self.assertFalse(any("Massive wall size" in r for r in signal.reasons))
        self.assertFalse(any("Aggressive active defending" in r for r in signal.reasons))

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_update_ce_wall_detected(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_persistence_snapshots = 3

        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24090.0,
            high=24095.0,
            low=24080.0,
            close=24085.0,
            volume=1000,
        )
        full_chain = [
            {
                "strike": 24100,
                "ce_oi": 5000000,
                "ce_oi_prev": 4500000,
                "ce_oi_change_pct": 11.1,
                "pe_oi": 100000,
                "pe_oi_prev": 100000,
                "pe_oi_change_pct": 0.0,
            },
            {
                "strike": 24150,
                "ce_oi": 6000000,
                "ce_oi_prev": 5000000,
                "ce_oi_change_pct": 20.0,
                "pe_oi": 100000,
                "pe_oi_prev": 100000,
                "pe_oi_change_pct": 0.0,
            },
        ]

        bias = self.detector.update(spot=24080.0, full_chain=full_chain, candle=candle, levels=[])
        self.assertIsNotNone(bias)
        self.assertEqual(bias.wall_strike, 24100.0)
        self.assertEqual(bias.wall_option_type, "CE")
        self.assertEqual(bias.direction, Direction.BEARISH)
        self.assertEqual(bias.trade_option_type, "PE")
        self.assertEqual(bias.wall_oi, 5000000)
        self.assertEqual(bias.persistence_snapshots, 1)
        self.assertEqual(bias.state, "TRACKING")

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_update_pe_wall_detected(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_persistence_snapshots = 3

        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24020.0,
            high=24025.0,
            low=24010.0,
            close=24015.0,
            volume=1000,
        )
        full_chain = [
            {
                "strike": 24000,
                "ce_oi": 100000,
                "ce_oi_prev": 100000,
                "ce_oi_change_pct": 0.0,
                "pe_oi": 5000000,
                "pe_oi_prev": 4500000,
                "pe_oi_change_pct": 11.1,
            }
        ]

        bias = self.detector.update(spot=24020.0, full_chain=full_chain, candle=candle, levels=[])
        self.assertIsNotNone(bias)
        self.assertEqual(bias.wall_strike, 24000.0)
        self.assertEqual(bias.wall_option_type, "PE")
        self.assertEqual(bias.direction, Direction.BULLISH)
        self.assertEqual(bias.trade_option_type, "CE")
        self.assertEqual(bias.wall_oi, 5000000)
        self.assertEqual(bias.persistence_snapshots, 1)
        self.assertEqual(bias.state, "TRACKING")

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_update_persistence_increments_to_persistent(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_persistence_snapshots = 3

        full_chain = [
            {
                "strike": 24100,
                "ce_oi": 5000000,
                "ce_oi_prev": 4500000,
                "ce_oi_change_pct": 11.1,
                "pe_oi": 100000,
                "pe_oi_prev": 100000,
                "pe_oi_change_pct": 0.0,
            }
        ]
        candle1 = OHLCVCandle(timestamp=datetime.now(), open=24075.0, high=24080.0, low=24070.0, close=24075.0, volume=1000)
        candle2 = OHLCVCandle(timestamp=datetime.now(), open=24075.0, high=24082.0, low=24072.0, close=24078.0, volume=1000)
        candle3 = OHLCVCandle(timestamp=datetime.now(), open=24078.0, high=24085.0, low=24074.0, close=24080.0, volume=1000)

        bias1 = self.detector.update(spot=24075.0, full_chain=full_chain, candle=candle1, levels=[])
        self.assertEqual(bias1.persistence_snapshots, 1)
        self.assertEqual(bias1.state, "TRACKING")

        bias2 = self.detector.update(spot=24078.0, full_chain=full_chain, candle=candle2, levels=[])
        self.assertEqual(bias2.persistence_snapshots, 2)
        self.assertEqual(bias2.state, "TRACKING")

        bias3 = self.detector.update(spot=24080.0, full_chain=full_chain, candle=candle3, levels=[])
        self.assertEqual(bias3.persistence_snapshots, 3)
        self.assertEqual(bias3.state, "PERSISTENT")

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_update_wall_identity_change_resets_persistence(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_persistence_snapshots = 3

        chain1 = [
            {"strike": 24100, "ce_oi": 5000000, "ce_oi_prev": 4500000, "ce_oi_change_pct": 11.1, "pe_oi": 100000, "pe_oi_prev": 100000, "pe_oi_change_pct": 0.0}
        ]
        chain2 = [
            {"strike": 24150, "ce_oi": 7000000, "ce_oi_prev": 6000000, "ce_oi_change_pct": 16.6, "pe_oi": 100000, "pe_oi_prev": 100000, "pe_oi_change_pct": 0.0}
        ]
        candle = OHLCVCandle(timestamp=datetime.now(), open=24075.0, high=24080.0, low=24070.0, close=24075.0, volume=1000)

        bias1 = self.detector.update(spot=24075.0, full_chain=chain1, candle=candle, levels=[])
        self.assertEqual(bias1.wall_strike, 24100.0)
        self.assertEqual(bias1.persistence_snapshots, 1)

        bias2 = self.detector.update(spot=24075.0, full_chain=chain1, candle=candle, levels=[])
        self.assertEqual(bias2.persistence_snapshots, 2)

        # Shift to 24150 wall
        bias3 = self.detector.update(spot=24075.0, full_chain=chain2, candle=candle, levels=[])
        self.assertEqual(bias3.wall_strike, 24150.0)
        self.assertEqual(bias3.persistence_snapshots, 1)
        self.assertEqual(bias3.state, "TRACKING")

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_update_no_qualifying_wall_returns_none(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_persistence_snapshots = 3

        candle = OHLCVCandle(timestamp=datetime.now(), open=24075.0, high=24080.0, low=24070.0, close=24075.0, volume=1000)

        # Empty chain
        self.assertIsNone(self.detector.update(spot=24075.0, full_chain=[], candle=candle, levels=[]))

        # Chain with OI below threshold
        chain_low_oi = [
            {"strike": 24100, "ce_oi": 2000000, "ce_oi_prev": 1900000, "ce_oi_change_pct": 10.0, "pe_oi": 100000, "pe_oi_prev": 100000, "pe_oi_change_pct": 0.0}
        ]
        self.assertIsNone(self.detector.update(spot=24075.0, full_chain=chain_low_oi, candle=candle, levels=[]))

        # Chain with OI change pct below threshold
        chain_low_change = [
            {"strike": 24100, "ce_oi": 5000000, "ce_oi_prev": 4900000, "ce_oi_change_pct": 2.0, "pe_oi": 100000, "pe_oi_prev": 100000, "pe_oi_change_pct": 0.0}
        ]
        self.assertIsNone(self.detector.update(spot=24075.0, full_chain=chain_low_change, candle=candle, levels=[]))

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_update_relative_percentile(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_persistence_snapshots = 3

        candle = OHLCVCandle(timestamp=datetime.now(), open=24075.0, high=24080.0, low=24070.0, close=24075.0, volume=1000)
        full_chain = [
            {"strike": 24100, "ce_oi": 5000000, "ce_oi_prev": 4500000, "ce_oi_change_pct": 11.1, "pe_oi": 100000, "pe_oi_prev": 100000, "pe_oi_change_pct": 0.0},
            {"strike": 24150, "ce_oi": 3000000, "ce_oi_prev": 3000000, "ce_oi_change_pct": 0.0, "pe_oi": 100000, "pe_oi_prev": 100000, "pe_oi_change_pct": 0.0},
            {"strike": 24200, "ce_oi": 2000000, "ce_oi_prev": 2000000, "ce_oi_change_pct": 0.0, "pe_oi": 100000, "pe_oi_prev": 100000, "pe_oi_change_pct": 0.0},
            {"strike": 24250, "ce_oi": 10000000, "ce_oi_prev": 9000000, "ce_oi_change_pct": 11.1, "pe_oi": 100000, "pe_oi_prev": 100000, "pe_oi_change_pct": 0.0},
        ]
        # Same-side non-zero strikes: 24100 (5M), 24150 (3M), 24200 (2M), 24250 (10M). Total 4.
        # Nearest qualifying is 24100 (5M). Strikes <= 5M: 24100, 24150, 24200 -> 3/4 = 75.0%
        bias = self.detector.update(spot=24075.0, full_chain=full_chain, candle=candle, levels=[])
        self.assertEqual(bias.relative_percentile, 75.0)


if __name__ == '__main__':
    unittest.main()

