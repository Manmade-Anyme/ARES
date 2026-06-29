import unittest
from unittest.mock import MagicMock, patch
from models import OHLCVCandle, SetupType, Direction, ResistanceLevel
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
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

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
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

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
    def test_oi_wall_detect_ce_rejection_success(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24090.0,
            high=24105.0,
            low=24080.0,
            close=24085.0,
            volume=1000
        )
        full_chain = [
            # Matches closest CE wall above spot
            {
                "strike": 24100,
                "ce_oi": 5000000,
                "ce_oi_prev": 4500000,
                "ce_oi_change_pct": 11.1,
                "pe_oi": 100000,
                "pe_oi_prev": 100000,
                "pe_oi_change_pct": 0.0
            },
            # Another CE wall further away
            {
                "strike": 24150,
                "ce_oi": 6000000,
                "ce_oi_prev": 5000000,
                "ce_oi_change_pct": 20.0,
                "pe_oi": 100000,
                "pe_oi_prev": 100000,
                "pe_oi_change_pct": 0.0
            }
        ]

        signal = self.detector.detect(spot=24080.0, full_chain=full_chain, candle=candle, levels=[])
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, Direction.BEARISH)
        self.assertEqual(signal.strike_to_trade, 24100)

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_detect_pe_bounce_success(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24010.0,
            high=24025.0,
            low=23995.0,
            close=24015.0,
            volume=1000
        )
        full_chain = [
            # Matches closest PE wall below spot
            {
                "strike": 24000,
                "ce_oi": 100000,
                "ce_oi_prev": 100000,
                "ce_oi_change_pct": 0.0,
                "pe_oi": 5000000,
                "pe_oi_prev": 4500000,
                "pe_oi_change_pct": 11.1
            },
            # Another PE wall further away
            {
                "strike": 23950,
                "ce_oi": 100000,
                "ce_oi_prev": 100000,
                "ce_oi_change_pct": 0.0,
                "pe_oi": 6000000,
                "pe_oi_prev": 5000000,
                "pe_oi_change_pct": 20.0
            }
        ]

        signal = self.detector.detect(spot=24020.0, full_chain=full_chain, candle=candle, levels=[])
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, Direction.BULLISH)
        self.assertEqual(signal.strike_to_trade, 24000)

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_detect_no_signal_conditions(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        # No walls matching min OI
        full_chain = [
            {
                "strike": 24100,
                "ce_oi": 1000000,  # Below min_oi
                "ce_oi_prev": 1000000,
                "ce_oi_change_pct": 0.0,
                "pe_oi": 1000000,
                "pe_oi_prev": 1000000,
                "pe_oi_change_pct": 0.0
            }
        ]
        candle = OHLCVCandle(
            timestamp=datetime.now(), open=24090.0, high=24105.0, low=24080.0, close=24085.0, volume=1000
        )
        self.assertIsNone(self.detector.detect(spot=24080.0, full_chain=full_chain, candle=candle, levels=[]))

        # Wall matches, but too far (spot distance > approach distance)
        full_chain[0]["ce_oi"] = 5000000
        full_chain[0]["ce_oi_change_pct"] = 10.0
        # Spot is 24000. CE wall 24100 is 100 pts away (> 80.0 approach distance)
        self.assertIsNone(self.detector.detect(spot=24000.0, full_chain=full_chain, candle=candle, levels=[]))

        # Wall matches and approaches, but not tested (candle.high < strike - test_distance)
        # Spot = 24080, strike = 24100. Candle high = 24075 (less than 24100 - 20 = 24080)
        candle_not_tested = OHLCVCandle(
            timestamp=datetime.now(), open=24070.0, high=24075.0, low=24060.0, close=24072.0, volume=1000
        )
        self.assertIsNone(self.detector.detect(spot=24080.0, full_chain=full_chain, candle=candle_not_tested, levels=[]))

        # Wall matches, approaches, tested, but not rejected (candle.close >= candle.open)
        candle_not_rejected = OHLCVCandle(
            timestamp=datetime.now(), open=24080.0, high=24105.0, low=24075.0, close=24085.0, volume=1000
        )
        self.assertIsNone(self.detector.detect(spot=24080.0, full_chain=full_chain, candle=candle_not_rejected, levels=[]))

        # Wall matches, approaches, tested, rejected, but writers covered (ce_oi < ce_oi_prev)
        full_chain_covered = [
            {
                "strike": 24100,
                "ce_oi": 5000000,
                "ce_oi_prev": 6000000,  # OI decreased!
                "ce_oi_change_pct": 10.0,
                "pe_oi": 100000,
                "pe_oi_prev": 100000,
                "pe_oi_change_pct": 0.0
            }
        ]
        self.assertIsNone(self.detector.detect(spot=24080.0, full_chain=full_chain_covered, candle=candle, levels=[]))

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_dynamic_targets_bullish(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24010.0,
            high=24025.0,
            low=23995.0,
            close=24015.0,
            volume=1000
        )
        wall = {
            "strike": 24000,
            "pe_oi": 5000000,
            "pe_oi_prev": 4500000,
            "pe_oi_change_pct": 11.1
        }
        
        # Spot is 24015. Dynamic resistances at 24080 and 24120. Both are >= 20 points from 24015.
        levels = [
            ResistanceLevel(price=24080.0, source="prev_day_high", strength=3),
            ResistanceLevel(price=24120.0, source="oi_wall_ce", strength=2),
            ResistanceLevel(price=23980.0, source="oi_wall_pe", strength=2), # Below entry, should be ignored for BULLISH target
        ]

        signal = self.detector._build_signal(
            candle=candle,
            spot=24020.0,
            wall=wall,
            direction=Direction.BULLISH,
            option_type="CE",
            levels=levels
        )
        
        self.assertEqual(signal.target_1, 24080.0)
        self.assertEqual(signal.target_2, 24120.0)
        self.assertTrue(any("Target 1 set at structural resistance" in r for r in signal.reasons))
        self.assertTrue(any("Target 2 set at structural resistance" in r for r in signal.reasons))

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_dynamic_targets_bearish(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24090.0,
            high=24105.0,
            low=24080.0,
            close=24085.0,
            volume=1000
        )
        wall = {
            "strike": 24100,
            "ce_oi": 5000000,
            "ce_oi_prev": 4500000,
            "ce_oi_change_pct": 11.1
        }
        
        # Spot is 24085. Dynamic supports at 24050 and 24010. Both are >= 20 points from 24085.
        # Sorted for BEARISH (supports descending)
        levels = [
            ResistanceLevel(price=24050.0, source="oi_wall_pe", strength=2),
            ResistanceLevel(price=24010.0, source="prev_day_low", strength=3),
            ResistanceLevel(price=24130.0, source="prev_day_high", strength=3), # Above entry, ignored for BEARISH target
        ]

        signal = self.detector._build_signal(
            candle=candle,
            spot=24080.0,
            wall=wall,
            direction=Direction.BEARISH,
            option_type="PE",
            levels=levels
        )
        
        self.assertEqual(signal.target_1, 24050.0)
        self.assertEqual(signal.target_2, 24010.0)
        self.assertTrue(any("Target 1 set at structural support" in r for r in signal.reasons))
        self.assertTrue(any("Target 2 set at structural support" in r for r in signal.reasons))

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_dynamic_targets_fallback(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        candle = OHLCVCandle(
            timestamp=datetime.now(),
            open=24010.0,
            high=24025.0,
            low=23995.0,
            close=24015.0,
            volume=1000
        )
        wall = {
            "strike": 24000,
            "pe_oi": 5000000,
            "pe_oi_prev": 4500000,
            "pe_oi_change_pct": 11.1
        }
        
        # Spot is 24015. Level is at 24020, which is too close (< 20 points away).
        levels = [
            ResistanceLevel(price=24020.0, source="prev_day_high", strength=3)
        ]

        signal = self.detector._build_signal(
            candle=candle,
            spot=24020.0,
            wall=wall,
            direction=Direction.BULLISH,
            option_type="CE",
            levels=levels
        )
        
        # Should fallback to fixed points: close + 35 = 24050, close + 70 = 24085
        self.assertEqual(signal.target_1, 24050.0)
        self.assertEqual(signal.target_2, 24085.0)

if __name__ == '__main__':
    unittest.main()
