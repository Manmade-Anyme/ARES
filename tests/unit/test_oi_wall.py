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
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
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
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
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
    def test_oi_wall_update_ce_rejection_confirmed(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        # Candle 1: candidate touch with a genuine upper-wick rejection
        candle1 = OHLCVCandle(
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

        # Candle 1 only forms a candidate; no signal yet.
        signal1 = self.detector.update(spot=24080.0, full_chain=full_chain, candle=candle1, levels=[])
        self.assertIsNone(signal1)

        # Candle 2: confirms by closing below candle 1's low (24080.0)
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24078.0, high=24082.0, low=24065.0, close=24070.0, volume=1000
        )
        signal2 = self.detector.update(spot=24070.0, full_chain=full_chain, candle=candle2, levels=[])
        self.assertIsNotNone(signal2)
        self.assertEqual(signal2.direction, Direction.BEARISH)
        # strike_to_trade is derived from spot at confirmation time (candle 2), not candle 1
        self.assertEqual(signal2.strike_to_trade, 24050)

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_update_ce_rejection_not_confirmed(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        candle1 = OHLCVCandle(
            timestamp=datetime.now(), open=24090.0, high=24105.0, low=24080.0, close=24085.0, volume=1000
        )
        full_chain = [
            {
                "strike": 24100,
                "ce_oi": 5000000,
                "ce_oi_prev": 4500000,
                "ce_oi_change_pct": 11.1,
                "pe_oi": 100000,
                "pe_oi_prev": 100000,
                "pe_oi_change_pct": 0.0
            }
        ]

        signal1 = self.detector.update(spot=24080.0, full_chain=full_chain, candle=candle1, levels=[])
        self.assertIsNone(signal1)

        # Candle 2 does NOT close below candle 1's low (24080.0) -> setup fails to confirm
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24082.0, high=24090.0, low=24081.0, close=24088.0, volume=1000
        )
        signal2 = self.detector.update(spot=24085.0, full_chain=full_chain, candle=candle2, levels=[])
        self.assertIsNone(signal2)

        # And the pending setup must have expired: a 3rd candle that would have
        # confirmed candle1 must NOT trigger a stale signal.
        candle3 = OHLCVCandle(
            timestamp=datetime.now(), open=24085.0, high=24086.0, low=24060.0, close=24065.0, volume=1000
        )
        signal3 = self.detector.update(spot=24065.0, full_chain=full_chain, candle=candle3, levels=[])
        self.assertIsNone(signal3)

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_update_pe_bounce_confirmed(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        # Candle 1: candidate touch with a genuine lower-wick rejection (bounce)
        candle1 = OHLCVCandle(
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

        signal1 = self.detector.update(spot=24020.0, full_chain=full_chain, candle=candle1, levels=[])
        self.assertIsNone(signal1)

        # Candle 2: confirms by closing above candle 1's high (24025.0)
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24026.0, high=24040.0, low=24024.0, close=24035.0, volume=1000
        )
        signal2 = self.detector.update(spot=24030.0, full_chain=full_chain, candle=candle2, levels=[])
        self.assertIsNotNone(signal2)
        self.assertEqual(signal2.direction, Direction.BULLISH)
        # strike_to_trade is derived from spot at confirmation time (candle 2), not candle 1
        self.assertEqual(signal2.strike_to_trade, 24050)

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_update_pe_bounce_not_confirmed(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        candle1 = OHLCVCandle(
            timestamp=datetime.now(), open=24010.0, high=24025.0, low=23995.0, close=24015.0, volume=1000
        )
        full_chain = [
            {
                "strike": 24000,
                "ce_oi": 100000,
                "ce_oi_prev": 100000,
                "ce_oi_change_pct": 0.0,
                "pe_oi": 5000000,
                "pe_oi_prev": 4500000,
                "pe_oi_change_pct": 11.1
            }
        ]

        signal1 = self.detector.update(spot=24020.0, full_chain=full_chain, candle=candle1, levels=[])
        self.assertIsNone(signal1)

        # Candle 2 does NOT close above candle 1's high (24025.0) -> setup fails to confirm
        candle2 = OHLCVCandle(
            timestamp=datetime.now(), open=24016.0, high=24024.0, low=24010.0, close=24012.0, volume=1000
        )
        signal2 = self.detector.update(spot=24015.0, full_chain=full_chain, candle=candle2, levels=[])
        self.assertIsNone(signal2)

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_update_no_candidate_without_wick_rejection(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        full_chain = [
            {
                "strike": 24100,
                "ce_oi": 5000000,
                "ce_oi_prev": 4500000,
                "ce_oi_change_pct": 11.1,
                "pe_oi": 100000,
                "pe_oi_prev": 100000,
                "pe_oi_change_pct": 0.0
            }
        ]

        # Candle closes red and touches the wall (high=24081 >= strike - test_distance=24080),
        # but the upper wick is small relative to the range (ratio 0.33 < 0.4) -> no genuine
        # rejection -> no candidate stored.
        candle = OHLCVCandle(
            timestamp=datetime.now(), open=24075.0, high=24081.0, low=24063.0, close=24065.0, volume=1000
        )
        signal = self.detector.update(spot=24080.0, full_chain=full_chain, candle=candle, levels=[])
        self.assertIsNone(signal)

        # Confirm no pending setup was stored by feeding a candle that would
        # otherwise confirm a bearish setup.
        candle_next = OHLCVCandle(
            timestamp=datetime.now(), open=24060.0, high=24062.0, low=24040.0, close=24045.0, volume=1000
        )
        signal_next = self.detector.update(spot=24045.0, full_chain=full_chain, candle=candle_next, levels=[])
        self.assertIsNone(signal_next)

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_update_no_signal_conditions(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
        mock_settings.strike_interval = 50
        mock_settings.entry_zone_offset_pts = 5.0
        mock_settings.target_1_pts = 35.0
        mock_settings.target_2_pts = 70.0

        candle = OHLCVCandle(
            timestamp=datetime.now(), open=24090.0, high=24105.0, low=24080.0, close=24085.0, volume=1000
        )

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
        self.assertIsNone(OIWallDetector().update(spot=24080.0, full_chain=full_chain, candle=candle, levels=[]))

        # Wall matches, but too far (spot distance > approach distance)
        full_chain[0]["ce_oi"] = 5000000
        full_chain[0]["ce_oi_change_pct"] = 10.0
        # Spot is 24000. CE wall 24100 is 100 pts away (> 80.0 approach distance)
        self.assertIsNone(OIWallDetector().update(spot=24000.0, full_chain=full_chain, candle=candle, levels=[]))

        # Wall matches and approaches, but not tested (candle.high < strike - test_distance)
        # Spot = 24080, strike = 24100. Candle high = 24075 (less than 24100 - 20 = 24080)
        candle_not_tested = OHLCVCandle(
            timestamp=datetime.now(), open=24070.0, high=24075.0, low=24060.0, close=24072.0, volume=1000
        )
        self.assertIsNone(OIWallDetector().update(spot=24080.0, full_chain=full_chain, candle=candle_not_tested, levels=[]))

        # Wall matches, approaches, tested, but not rejected (candle.close >= candle.open)
        candle_not_rejected = OHLCVCandle(
            timestamp=datetime.now(), open=24080.0, high=24105.0, low=24075.0, close=24085.0, volume=1000
        )
        self.assertIsNone(OIWallDetector().update(spot=24080.0, full_chain=full_chain, candle=candle_not_rejected, levels=[]))

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
        self.assertIsNone(OIWallDetector().update(spot=24080.0, full_chain=full_chain_covered, candle=candle, levels=[]))

    @patch('detectors.oi_wall.settings')
    def test_oi_wall_dynamic_targets_bullish(self, mock_settings):
        mock_settings.oi_wall_min_oi = 4000000
        mock_settings.oi_wall_min_oi_change_pct = 5.0
        mock_settings.oi_wall_approach_distance = 80.0
        mock_settings.oi_wall_test_distance = 20.0
        mock_settings.oi_wall_stop_buffer = 25.0
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
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
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
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
        mock_settings.oi_wall_wick_rejection_ratio = 0.4
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
