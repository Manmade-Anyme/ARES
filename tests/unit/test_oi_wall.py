import unittest
from unittest.mock import MagicMock, patch
from models import OHLCVCandle, SetupType, Direction
from datetime import datetime
from detectors.oi_wall import OIWallDetector

class TestOIWallDetector(unittest.TestCase):
    def setUp(self):
        self.detector = OIWallDetector()

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_confidence_high(self, mock_settings):
        # Set up settings thresholds
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0

        # Case 1: High Confidence (score >= 2: massive wall and deep test/penetration)
        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24095.0,
            high=24105.0,  # Deep test of 24100
            low=24090.0,
            close=24092.0,  # Bearish rejection candle
            volume=1000
        )
        wall = {
            "strike": 24100,
            "ce_oi": 6000000,          # 1.5x of min_oi (4000000) -> 1 point
            "ce_oi_prev": 5900000,
            "ce_oi_change_pct": 2.0,  # Less than 1.5x of min_oi_change_pct (5.0) -> 0 points
            "pe_oi": 1000000,
            "pe_oi_prev": 1000000,
            "pe_oi_change_pct": 0.0
        }

        # Spot is 24090. CE wall 24100 is above spot. Direction: BEARISH
        signal = self.detector._build_signal(
            candle=candle,
            spot=24090.0,
            wall=wall,
            direction=Direction.BEARISH,
            option_type="PE"
        )
        self.assertEqual(signal.confidence, "HIGH")
        # Ensure it contains specific reasons for the score points met
        self.assertTrue(any("Massive wall size" in r for r in signal.reasons))
        self.assertTrue(any("tested wall deeply" in r for r in signal.reasons))
        self.assertFalse(any("Aggressive active defending" in r for r in signal.reasons))

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_confidence_medium(self, mock_settings):
        # Set up settings thresholds
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0

        # Case 2: Medium Confidence (score < 2: only pierced condition met)
        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24101.0,
            high=24101.0,  # Pierces 24100 -> 1 point
            low=24094.0,
            close=24095.0,  # Bearish rejection candle but open is at the high (no upper wick) -> 0 points
            volume=1000
        )
        wall = {
            "strike": 24100,
            "ce_oi": 4100000,          # Less than 1.5x of min_oi -> 0 points
            "ce_oi_prev": 4000000,
            "ce_oi_change_pct": 2.5,  # Less than 1.5x of min_change_pct -> 0 points
            "pe_oi": 1000000,
            "pe_oi_prev": 1000000,
            "pe_oi_change_pct": 0.0
        }

        signal = self.detector._build_signal(
            candle=candle,
            spot=24090.0,
            wall=wall,
            direction=Direction.BEARISH,
            option_type="PE"
        )
        self.assertEqual(signal.confidence, "MEDIUM")
        self.assertTrue(any("tested wall deeply" in r for r in signal.reasons))
        self.assertFalse(any("Massive wall size" in r for r in signal.reasons))
        self.assertFalse(any("Aggressive active defending" in r for r in signal.reasons))

if __name__ == '__main__':
    unittest.main()
