import unittest
from datetime import datetime, timedelta

from models import OHLCVCandle, Direction, OIWallBias, OIWallTelemetry, OIWallEntryDecision
from detectors.oi_wall_entry import OIWallEntryFilter


class TestOIWallEntryFilter(unittest.TestCase):
    def setUp(self):
        self.filter = OIWallEntryFilter()
        self.t0 = datetime(2026, 9, 5, 9, 30, 0)

    def _make_ce_bias(self, strike=24100.0, persistence=1, state="TRACKING"):
        return OIWallBias(
            wall_key=f"CE:{int(strike)}",
            wall_strike=strike,
            wall_option_type="CE",
            direction=Direction.BEARISH,
            trade_option_type="PE",
            wall_oi=5000000,
            wall_oi_change_pct=10.0,
            relative_percentile=90.0,
            first_seen=self.t0,
            last_seen=self.t0,
            persistence_snapshots=persistence,
            persistence_duration_seconds=0.0,
            state=state,
            initial_interaction_timestamp=None,
            initial_interaction_price=None,
            favourable_excursion_pts=0.0,
            reasons=("CE wall test",),
        )

    def _make_pe_bias(self, strike=24000.0, persistence=1, state="TRACKING"):
        return OIWallBias(
            wall_key=f"PE:{int(strike)}",
            wall_strike=strike,
            wall_option_type="PE",
            direction=Direction.BULLISH,
            trade_option_type="CE",
            wall_oi=5000000,
            wall_oi_change_pct=10.0,
            relative_percentile=90.0,
            first_seen=self.t0,
            last_seen=self.t0,
            persistence_snapshots=persistence,
            persistence_duration_seconds=0.0,
            state=state,
            initial_interaction_timestamp=None,
            initial_interaction_price=None,
            favourable_excursion_pts=0.0,
            reasons=("PE wall test",),
        )

    def test_no_wall_when_bias_is_none(self):
        candle = OHLCVCandle(timestamp=self.t0, open=24050.0, high=24060.0, low=24040.0, close=24055.0, volume=1000)
        decision = self.filter.update(bias=None, candle=candle, levels=[])
        self.assertEqual(decision.status, "NO_WALL")
        self.assertEqual(decision.telemetry.filter_state, "NO_WALL")
        self.assertIsNone(decision.wall_key)
        self.assertIsNone(decision.decision_id)
        self.assertIsNone(decision.bias)

    def test_tracking_and_persistent_without_interaction(self):
        # Candle far away from wall 24100 (high=24050 < 24100 - 20)
        candle = OHLCVCandle(timestamp=self.t0, open=24040.0, high=24050.0, low=24030.0, close=24045.0, volume=1000)
        bias1 = self._make_ce_bias(persistence=1)
        decision1 = self.filter.update(bias=bias1, candle=candle, levels=[])
        self.assertEqual(decision1.status, "WAITING")
        self.assertEqual(self.filter.state, "TRACKING")

        bias2 = self._make_ce_bias(persistence=2)
        decision2 = self.filter.update(bias=bias2, candle=candle, levels=[])
        self.assertEqual(decision2.status, "WAITING")
        self.assertEqual(self.filter.state, "TRACKING")

        bias3 = self._make_ce_bias(persistence=3)
        decision3 = self.filter.update(bias=bias3, candle=candle, levels=[])
        self.assertEqual(decision3.status, "WAITING")
        self.assertEqual(self.filter.state, "PERSISTENT")

    def test_ce_wall_full_lifecycle_to_qualified_and_consumed(self):
        # 1. Snapshot 1: Interaction occurs (high reaches 24085 >= 24100 - 20, defended close at 24075 <= 24100)
        candle1 = OHLCVCandle(timestamp=self.t0, open=24060.0, high=24085.0, low=24055.0, close=24075.0, volume=1000)
        bias1 = self._make_ce_bias(persistence=1)
        decision1 = self.filter.update(bias=bias1, candle=candle1, levels=[])
        self.assertEqual(decision1.status, "WAITING")
        self.assertEqual(self.filter.state, "INTERACTED")
        self.assertEqual(self.filter.initial_interaction_timestamp, self.t0)
        self.assertEqual(self.filter.initial_interaction_price, 24075.0)

        # 2. Snapshot 2: Persistence increments to 2, candle continues down
        t1 = self.t0 + timedelta(minutes=1)
        candle2 = OHLCVCandle(timestamp=t1, open=24070.0, high=24072.0, low=24050.0, close=24055.0, volume=1000)
        bias2 = self._make_ce_bias(persistence=2)
        decision2 = self.filter.update(bias=bias2, candle=candle2, levels=[])
        self.assertEqual(self.filter.state, "INTERACTED")
        self.assertAlmostEqual(self.filter.favourable_excursion_pts, 50.0)  # 24100 - 24050 = 50.0

        # 3. Snapshot 3: Persistence reaches 3, excursion (50 >= 12) -> transitions to RETEST_READY and emits watchlist alert
        t2 = self.t0 + timedelta(minutes=2)
        candle3 = OHLCVCandle(timestamp=t2, open=24055.0, high=24060.0, low=24045.0, close=24050.0, volume=1000)
        bias3 = self._make_ce_bias(persistence=3)
        decision3 = self.filter.update(bias=bias3, candle=candle3, levels=[])
        self.assertEqual(self.filter.state, "RETEST_READY")
        self.assertIsNotNone(self.filter.latest_watchlist_event)
        self.assertEqual(self.filter.latest_watchlist_event.wall_key, "CE:24100")

        # Snapshot 4: Still in RETEST_READY, watchlist alert must not repeat for same key
        t3 = self.t0 + timedelta(minutes=3)
        candle4 = OHLCVCandle(timestamp=t3, open=24050.0, high=24065.0, low=24048.0, close=24060.0, volume=1000)
        bias4 = self._make_ce_bias(persistence=4)
        decision4 = self.filter.update(bias=bias4, candle=candle4, levels=[])
        self.assertEqual(self.filter.state, "RETEST_READY")
        self.assertIsNone(self.filter.latest_watchlist_event)

        # 4. Secondary Re-test Candle: reaches within 20 pts of 24100 (high=24088 >= 24080) and defends (close=24078 <= 24100)
        t4 = self.t0 + timedelta(minutes=4)
        candle5 = OHLCVCandle(timestamp=t4, open=24065.0, high=24088.0, low=24062.0, close=24078.0, volume=1000)
        bias5 = self._make_ce_bias(persistence=5)
        decision5 = self.filter.update(bias=bias5, candle=candle5, levels=[])
        self.assertEqual(decision5.status, "QUALIFIED")
        self.assertEqual(self.filter.state, "QUALIFIED")
        self.assertIsNotNone(decision5.decision_id)
        self.assertEqual(decision5.trigger_price, 24078.0)

        # Acknowledge EMITTED -> transitions to CONSUMED
        ack_decision = self.filter.acknowledge(decision5, "EMITTED")
        self.assertEqual(ack_decision.status, "CONSUMED")
        self.assertEqual(self.filter.state, "CONSUMED")
        self.assertIn("CE:24100", self.filter.consumed_wall_keys)

        # Subsequent candle on same wall key remains CONSUMED
        t5 = self.t0 + timedelta(minutes=5)
        candle6 = OHLCVCandle(timestamp=t5, open=24075.0, high=24080.0, low=24060.0, close=24065.0, volume=1000)
        bias6 = self._make_ce_bias(persistence=6)
        decision6 = self.filter.update(bias=bias6, candle=candle6, levels=[])
        self.assertEqual(decision6.status, "CONSUMED")

    def test_pe_wall_full_lifecycle_to_qualified(self):
        # Bullish PE wall 24000. Approach from above, bounce, re-test
        # 1. Interaction: low drops to 24015 <= 24000 + 20, defends close at 24025 >= 24000
        candle1 = OHLCVCandle(timestamp=self.t0, open=24040.0, high=24045.0, low=24015.0, close=24025.0, volume=1000)
        bias1 = self._make_pe_bias(persistence=1)
        decision1 = self.filter.update(bias=bias1, candle=candle1, levels=[])
        self.assertEqual(self.filter.state, "INTERACTED")

        # 2. Excursion: high climbs to 24060 (60 pts above 24000 >= 12)
        t1 = self.t0 + timedelta(minutes=1)
        candle2 = OHLCVCandle(timestamp=t1, open=24025.0, high=24060.0, low=24022.0, close=24055.0, volume=1000)
        bias2 = self._make_pe_bias(persistence=2)
        decision2 = self.filter.update(bias=bias2, candle=candle2, levels=[])
        self.assertEqual(self.filter.state, "INTERACTED")

        # 3. Snapshot 3: persistence reaches 3 -> RETEST_READY
        t2 = self.t0 + timedelta(minutes=2)
        candle3 = OHLCVCandle(timestamp=t2, open=24055.0, high=24065.0, low=24045.0, close=24050.0, volume=1000)
        bias3 = self._make_pe_bias(persistence=3)
        decision3 = self.filter.update(bias=bias3, candle=candle3, levels=[])
        self.assertEqual(self.filter.state, "RETEST_READY")

        # 4. Secondary Re-test: low pulls back to 24018 <= 24020, closes defended at 24028 >= 24000
        t3 = self.t0 + timedelta(minutes=3)
        candle4 = OHLCVCandle(timestamp=t3, open=24045.0, high=24048.0, low=24018.0, close=24028.0, volume=1000)
        bias4 = self._make_pe_bias(persistence=4)
        decision4 = self.filter.update(bias=bias4, candle=candle4, levels=[])
        self.assertEqual(decision4.status, "QUALIFIED")
        self.assertEqual(decision4.bias.direction, Direction.BULLISH)

    def test_retest_breach_invalidates_to_expired(self):
        # CE wall 24100. Reach RETEST_READY, but secondary re-test closes ABOVE 24100 (breach)
        candle1 = OHLCVCandle(timestamp=self.t0, open=24070.0, high=24085.0, low=24065.0, close=24075.0, volume=1000)
        self.filter.update(bias=self._make_ce_bias(persistence=1), candle=candle1, levels=[])

        t1 = self.t0 + timedelta(minutes=1)
        candle2 = OHLCVCandle(timestamp=t1, open=24075.0, high=24078.0, low=24050.0, close=24055.0, volume=1000)
        self.filter.update(bias=self._make_ce_bias(persistence=2), candle=candle2, levels=[])

        t2 = self.t0 + timedelta(minutes=2)
        candle3 = OHLCVCandle(timestamp=t2, open=24055.0, high=24060.0, low=24045.0, close=24050.0, volume=1000)
        self.filter.update(bias=self._make_ce_bias(persistence=3), candle=candle3, levels=[])
        self.assertEqual(self.filter.state, "RETEST_READY")

        # Secondary re-test closes at 24105 (> 24100) -> breach!
        t3 = self.t0 + timedelta(minutes=3)
        candle4 = OHLCVCandle(timestamp=t3, open=24080.0, high=24110.0, low=24075.0, close=24105.0, volume=1000)
        decision4 = self.filter.update(bias=self._make_ce_bias(persistence=4), candle=candle4, levels=[])
        self.assertEqual(decision4.status, "EXPIRED")
        self.assertEqual(self.filter.state, "EXPIRED")
        self.assertIn("breach", decision4.rejection_reason.lower())

    def test_acknowledgement_cooldown_returns_to_retest_ready(self):
        # Setup qualifies
        candle1 = OHLCVCandle(timestamp=self.t0, open=24070.0, high=24085.0, low=24065.0, close=24075.0, volume=1000)
        self.filter.update(bias=self._make_ce_bias(persistence=1), candle=candle1, levels=[])
        t1 = self.t0 + timedelta(minutes=1)
        candle2 = OHLCVCandle(timestamp=t1, open=24075.0, high=24078.0, low=24050.0, close=24055.0, volume=1000)
        self.filter.update(bias=self._make_ce_bias(persistence=2), candle=candle2, levels=[])
        t2 = self.t0 + timedelta(minutes=2)
        candle3 = OHLCVCandle(timestamp=t2, open=24055.0, high=24060.0, low=24045.0, close=24050.0, volume=1000)
        self.filter.update(bias=self._make_ce_bias(persistence=3), candle=candle3, levels=[])
        t3 = self.t0 + timedelta(minutes=3)
        candle4 = OHLCVCandle(timestamp=t3, open=24065.0, high=24088.0, low=24062.0, close=24078.0, volume=1000)
        qualified_decision = self.filter.update(bias=self._make_ce_bias(persistence=4), candle=candle4, levels=[])
        self.assertEqual(qualified_decision.status, "QUALIFIED")

        # Engine suppresses due to cooldown
        ack = self.filter.acknowledge(qualified_decision, "SUPPRESSED_BY_COOLDOWN")
        self.assertEqual(ack.status, "WAITING")
        self.assertEqual(self.filter.state, "RETEST_READY")
        self.assertIsNone(self.filter.retest_timestamp)

    def test_acknowledgement_rr_rejection_expires(self):
        # Setup qualifies
        candle1 = OHLCVCandle(timestamp=self.t0, open=24070.0, high=24085.0, low=24065.0, close=24075.0, volume=1000)
        self.filter.update(bias=self._make_ce_bias(persistence=1), candle=candle1, levels=[])
        t1 = self.t0 + timedelta(minutes=1)
        candle2 = OHLCVCandle(timestamp=t1, open=24075.0, high=24078.0, low=24050.0, close=24055.0, volume=1000)
        self.filter.update(bias=self._make_ce_bias(persistence=2), candle=candle2, levels=[])
        t2 = self.t0 + timedelta(minutes=2)
        candle3 = OHLCVCandle(timestamp=t2, open=24055.0, high=24060.0, low=24045.0, close=24050.0, volume=1000)
        self.filter.update(bias=self._make_ce_bias(persistence=3), candle=candle3, levels=[])
        t3 = self.t0 + timedelta(minutes=3)
        candle4 = OHLCVCandle(timestamp=t3, open=24065.0, high=24088.0, low=24062.0, close=24078.0, volume=1000)
        qualified_decision = self.filter.update(bias=self._make_ce_bias(persistence=4), candle=candle4, levels=[])

        # Engine rejects due to R:R
        ack = self.filter.acknowledge(qualified_decision, "REJECTED_BY_RR")
        self.assertEqual(ack.status, "EXPIRED")
        self.assertEqual(self.filter.state, "EXPIRED")

    def test_wall_identity_change_resets_filter_state(self):
        # Setup interaction on 24100 CE
        candle1 = OHLCVCandle(timestamp=self.t0, open=24070.0, high=24085.0, low=24065.0, close=24075.0, volume=1000)
        self.filter.update(bias=self._make_ce_bias(strike=24100.0, persistence=1), candle=candle1, levels=[])
        self.assertEqual(self.filter.state, "INTERACTED")

        # Shift to 24150 CE
        t1 = self.t0 + timedelta(minutes=1)
        candle2 = OHLCVCandle(timestamp=t1, open=24075.0, high=24080.0, low=24070.0, close=24075.0, volume=1000)
        self.filter.update(bias=self._make_ce_bias(strike=24150.0, persistence=1), candle=candle2, levels=[])
        self.assertEqual(self.filter.state, "TRACKING")
        self.assertIsNone(self.filter.initial_interaction_timestamp)
        self.assertEqual(self.filter.current_wall_key, "CE:24150")


if __name__ == "__main__":
    unittest.main()
