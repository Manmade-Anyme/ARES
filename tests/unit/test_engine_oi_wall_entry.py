import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from config import settings
from config_profiles import NON_EXPIRY_CONFIG
from engine import AresEngine
from models import OHLCVCandle, ATMStrikes, AresSignal, SetupType, Direction, OIWallBias, OIWallTelemetry, OIWallEntryDecision


class TestEngineOIWallEntry(unittest.TestCase):
    def setUp(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)
        self.engine = AresEngine()
        self.t0 = datetime(2026, 9, 5, 9, 30, 0)

    def tearDown(self):
        settings.apply_profile(NON_EXPIRY_CONFIG)

    def _make_candle(self, close=24050.0):
        candle = MagicMock(spec=OHLCVCandle)
        candle.open = close
        candle.high = close + 5.0
        candle.low = close - 5.0
        candle.close = close
        candle.volume = 100000
        candle.vwap = close
        candle.timestamp = self.t0
        return candle

    def _make_atm(self, spot=24050.0):
        atm = MagicMock(spec=ATMStrikes)
        atm.ce = MagicMock()
        atm.ce.iv = 12.0
        atm.ce.oi = 1000000
        atm.ce.oi_prev = 950000
        atm.pe = MagicMock()
        atm.pe.iv = 12.0
        atm.pe.oi = 1000000
        atm.pe.oi_prev = 950000
        atm.spot_price = spot
        return atm

    def _make_bias(self, strike=24100.0, state="RETEST_READY"):
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
            persistence_snapshots=3,
            persistence_duration_seconds=0.0,
            state=state,
            initial_interaction_timestamp=self.t0,
            initial_interaction_price=24080.0,
            favourable_excursion_pts=25.0,
            reasons=("CE wall test",),
        )

    def _make_decision(self, status="QUALIFIED", bias=None):
        if bias is None:
            bias = self._make_bias()
        telemetry = OIWallTelemetry(
            bias=bias,
            entry_status=status,
            filter_state=status,
            rejection_reason=None,
            initial_interaction_timestamp=bias.initial_interaction_timestamp,
            initial_interaction_price=bias.initial_interaction_price,
            favourable_excursion_pts=bias.favourable_excursion_pts,
            retest_timestamp=self.t0,
            reference_price=bias.wall_strike,
            vwap=24050.0,
            opening_range=None,
        )
        return OIWallEntryDecision(
            status=status,
            wall_key=bias.wall_key,
            decision_id=f"{bias.wall_key}:12345",
            bias=bias,
            telemetry=telemetry,
            trigger_price=24075.0,
            retest_timestamp=self.t0,
            rejection_reason=None,
            reference_price=bias.wall_strike,
        )

    def test_advances_during_cooldown_and_acknowledges_suppressed(self):
        bias = self._make_bias()
        decision = self._make_decision(status="QUALIFIED", bias=bias)

        self.engine.oi_wall_detector.update = MagicMock(return_value=bias)
        self.engine.oi_wall_filter.update = MagicMock(return_value=decision)
        self.engine.oi_wall_filter.acknowledge = MagicMock(
            return_value=self._make_decision(status="WAITING", bias=bias)
        )

        # Set engine in cooldown
        self.engine.last_signal_time = datetime.now() - timedelta(minutes=2)

        candle = self._make_candle(24075.0)
        atm = self._make_atm(24075.0)
        signal = self.engine.tick(candle, [], atm, 0.0, [])

        self.assertIsNone(signal)
        self.engine.oi_wall_detector.update.assert_called_once()
        self.engine.oi_wall_filter.update.assert_called_once()
        self.engine.oi_wall_filter.acknowledge.assert_called_once_with(decision, "SUPPRESSED_BY_COOLDOWN")

    def test_advances_during_breakout_priority_and_acknowledges_suppressed(self):
        bias = self._make_bias()
        decision = self._make_decision(status="QUALIFIED", bias=bias)

        breakout_sig = AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=Direction.BEARISH,
            trigger_price=24070.0,
            entry_zone=(24065.0, 24075.0),
            stop_loss=24086.0,
            target_1=24035.0,
            target_2=24000.0,
            confidence="HIGH",
            reasons=["Breakout failed"],
            timestamp=self.t0,
            strike_to_trade=24050,
            option_type="PE",
        )

        self.engine.oi_wall_detector.update = MagicMock(return_value=bias)
        self.engine.oi_wall_filter.update = MagicMock(return_value=decision)
        self.engine.oi_wall_filter.acknowledge = MagicMock(
            return_value=self._make_decision(status="WAITING", bias=bias)
        )
        self.engine.breakout_detector.update = MagicMock(return_value=breakout_sig)

        candle = self._make_candle(24070.0)
        atm = self._make_atm(24070.0)
        signal = self.engine.tick(candle, [], atm, 0.0, [])

        self.assertIsNotNone(signal)
        self.assertEqual(signal.setup_type, SetupType.FAILED_BREAKOUT)
        self.engine.oi_wall_filter.acknowledge.assert_called_once_with(decision, "SUPPRESSED_BY_PRIORITY")

    def test_rr_rejection_acknowledges_rejected_by_rr(self):
        bias = self._make_bias()
        decision = self._make_decision(status="QUALIFIED", bias=bias)

        self.engine.oi_wall_detector.update = MagicMock(return_value=bias)
        self.engine.oi_wall_filter.update = MagicMock(return_value=decision)
        self.engine.oi_wall_filter.acknowledge = MagicMock(
            return_value=self._make_decision(status="EXPIRED", bias=bias)
        )

        # Force R:R rejection by emptying per_type_levels and returning inverted levels from detector
        settings.per_type_levels = {}
        mock_signal = AresSignal(
            setup_type=SetupType.OI_WALL_REJECTION,
            direction=Direction.BEARISH,
            trigger_price=24075.0,
            entry_zone=(24070.0, 24080.0),
            stop_loss=24150.0,   # Risk = 75 pts
            target_1=24055.0,    # Reward = 20 pts -> R:R < 1.0
            target_2=24020.0,
            confidence="HIGH",
            reasons=[],
            timestamp=self.t0,
            strike_to_trade=24050,
            option_type="PE",
        )
        self.engine.oi_wall_detector.build_signal = MagicMock(return_value=mock_signal)

        candle = self._make_candle(24075.0)
        atm = self._make_atm(24075.0)
        signal = self.engine.tick(candle, [], atm, 0.0, [])

        self.assertIsNone(signal)
        self.engine.oi_wall_filter.acknowledge.assert_called_once_with(decision, "REJECTED_BY_RR")

    def test_qualified_signal_emits_and_acknowledges_emitted(self):
        bias = self._make_bias()
        decision = self._make_decision(status="QUALIFIED", bias=bias)

        self.engine.oi_wall_detector.update = MagicMock(return_value=bias)
        self.engine.oi_wall_filter.update = MagicMock(return_value=decision)
        self.engine.oi_wall_filter.acknowledge = MagicMock(
            return_value=self._make_decision(status="CONSUMED", bias=bias)
        )

        candle = self._make_candle(24075.0)
        atm = self._make_atm(24075.0)
        signal = self.engine.tick(candle, [], atm, 0.0, [])

        self.assertIsNotNone(signal)
        self.assertEqual(signal.setup_type, SetupType.OI_WALL_REJECTION)
        self.engine.oi_wall_filter.acknowledge.assert_called_once_with(decision, "EMITTED")
        self.assertIsNotNone(signal.oi_wall_context)
        self.assertEqual(signal.oi_wall_context.get("wall_strike"), 24100.0)

    def test_latest_oi_wall_context_persisted_for_telemetry_even_without_signal(self):
        bias = self._make_bias(state="TRACKING")
        decision = self._make_decision(status="WAITING", bias=bias)

        self.engine.oi_wall_detector.update = MagicMock(return_value=bias)
        self.engine.oi_wall_filter.update = MagicMock(return_value=decision)

        candle = self._make_candle(24050.0)
        atm = self._make_atm(24050.0)
        signal = self.engine.tick(candle, [], atm, 0.0, [])

        self.assertIsNone(signal)
        ctx = self.engine.latest_oi_wall_context
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["wall_strike"], 24100.0)
        self.assertEqual(ctx["entry_status"], "WAITING")

    def test_vanished_wall_expiration_persisted_for_telemetry(self):
        bias = self._make_bias(state="INTERACTED")
        decision = self._make_decision(status="EXPIRED", bias=bias)

        self.engine.oi_wall_detector.update = MagicMock(return_value=None)
        self.engine.oi_wall_filter.update = MagicMock(return_value=decision)

        candle = self._make_candle(24050.0)
        atm = self._make_atm(24050.0)
        self.engine.tick(candle, [], atm, 0.0, [])

        self.assertEqual(self.engine.latest_oi_wall_context["entry_status"], "EXPIRED")


if __name__ == "__main__":
    unittest.main()
