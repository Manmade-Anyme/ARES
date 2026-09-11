from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from models import (
    OHLCVCandle,
    AresSignal,
    SetupType,
    Direction,
    ResistanceLevel,
    confidence_from_score,
    OIWallBias,
    OIWallTelemetry,
    OIWallEntryDecision,
)
from config import settings


def _setting_int(name: str, default: int) -> int:
    val = getattr(settings, name, None)
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    if isinstance(val, float):
        return int(val)
    return default


def _setting_float(name: str, default: float) -> float:
    val = getattr(settings, name, None)
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    return default


class OIWallDetector:
    """
    OIWallDetector identifies qualifying OI Wall barriers and maintains persistence state.

    Decoupled architecture (TASK-073):
    - `update()` scans the option chain for qualifying CE/PE walls, computes relative percentile,
      and tracks consecutive snapshot persistence, emitting an immutable `OIWallBias`.
      It does NOT emit an AresSignal directly.
    - `build_signal()` constructs the final `AresSignal` only from a qualified `OIWallEntryDecision`.
    """

    def __init__(self):
        self.current_wall_key: Optional[str] = None
        self.first_seen: Optional[datetime] = None
        self.last_seen: Optional[datetime] = None
        self.persistence_snapshots: int = 0
        self.has_interacted: bool = False

    def release_terminal_wall(self, wall_key: Optional[str]) -> None:
        """Release priority only when the filter terminates the tracked wall."""
        if wall_key != self.current_wall_key:
            return

        self.current_wall_key = None
        self.first_seen = None
        self.last_seen = None
        self.persistence_snapshots = 0
        self.has_interacted = False

    def register_interaction(self, wall_key: Optional[str]) -> None:
        """Explicitly record that the filter has confirmed interaction for this wall."""
        if wall_key and wall_key == self.current_wall_key:
            self.has_interacted = True

    @staticmethod
    def _check_candle_interaction(
        candle: OHLCVCandle,
        strike: float,
        wall_option_type: str,
        interaction_dist: float,
    ) -> bool:
        is_bearish = (wall_option_type == "CE")
        interacted = (
            (candle.high >= strike - interaction_dist)
            if is_bearish
            else (candle.low <= strike + interaction_dist)
        )
        defended = (candle.close <= strike) if is_bearish else (candle.close >= strike)
        return bool(interacted and defended)


    def update(
        self,
        spot: float,
        full_chain: List[Dict[str, Any]],
        candle: OHLCVCandle,
        levels: List[ResistanceLevel],
    ) -> Optional[OIWallBias]:
        """
        Scan the option chain for qualifying walls and update persistence.
        """
        nearest_ce_wall = None
        nearest_pe_wall = None

        min_oi = _setting_float("oi_wall_min_oi", 4000000.0)
        min_oi_change = _setting_float("oi_wall_min_oi_change_pct", 5.0)

        # 1 & 2: Find the nearest qualifying CE and PE walls
        for row in full_chain:
            strike = float(row["strike"])

            # CE Walls (Resistance, above spot, or actively tracked CE wall)
            is_tracked_ce = (
                self.current_wall_key is not None
                and self.current_wall_key == f"CE:{int(strike)}"
            )
            if strike > spot or is_tracked_ce:
                ce_oi = row["ce_oi"]
                ce_oi_change_pct = row["ce_oi_change_pct"]
                if ce_oi > min_oi and ce_oi_change_pct > min_oi_change:
                    if nearest_ce_wall is None or abs(strike - spot) < abs(float(nearest_ce_wall["strike"]) - spot):
                        nearest_ce_wall = row

            # PE Walls (Support, below spot, or actively tracked PE wall)
            is_tracked_pe = (
                self.current_wall_key is not None
                and self.current_wall_key == f"PE:{int(strike)}"
            )
            if strike < spot or is_tracked_pe:
                pe_oi = row["pe_oi"]
                pe_oi_change_pct = row["pe_oi_change_pct"]
                if pe_oi > min_oi and pe_oi_change_pct > min_oi_change:
                    if nearest_pe_wall is None or abs(strike - spot) < abs(float(nearest_pe_wall["strike"]) - spot):
                        nearest_pe_wall = row

        # Pick the qualifying wall to track.
        #
        # Priority rule (MANM-110): when both a CE and PE wall qualify simultaneously,
        # prefer the currently tracked wall to avoid resetting the OIWallEntryFilter
        # state machine mid-interaction/retest cycle.
        #
        # However, if spot has moved so far from the tracked wall that it falls outside
        # 2× the configured interaction distance, the tracked wall is no longer relevant
        # and the closer opposite-side wall correctly takes over.
        #
        # Same-side nearest-wall selection is always pure distance (preserves f410bdd
        # order-independence guarantee).
        selected_wall = None
        wall_option_type = None

        interaction_dist = _setting_float("oi_wall_initial_interaction_distance_pts", 20.0)
        proximity_window = 2.0 * interaction_dist  # tracked wall still "reachable" from spot

        tracked_ce = bool(
            nearest_ce_wall
            and self.current_wall_key == f"CE:{int(float(nearest_ce_wall['strike']))}"
        )
        tracked_pe = bool(
            nearest_pe_wall
            and self.current_wall_key == f"PE:{int(float(nearest_pe_wall['strike']))}"
        )

        if nearest_ce_wall and nearest_pe_wall:
            ce_dist = abs(float(nearest_ce_wall["strike"]) - spot)
            pe_dist = abs(spot - float(nearest_pe_wall["strike"]))

            ce_active = tracked_ce and (
                self.has_interacted
                or self._check_candle_interaction(candle, float(nearest_ce_wall["strike"]), "CE", interaction_dist)
            )
            pe_active = tracked_pe and (
                self.has_interacted
                or self._check_candle_interaction(candle, float(nearest_pe_wall["strike"]), "PE", interaction_dist)
            )

            ce_priority_window = proximity_window if ce_active else interaction_dist
            pe_priority_window = proximity_window if pe_active else interaction_dist

            if tracked_ce and ce_dist <= ce_priority_window:
                # Tracked CE wall is active or in initial interaction band — keep it.
                selected_wall = nearest_ce_wall
                wall_option_type = "CE"
            elif tracked_pe and pe_dist <= pe_priority_window:
                # Tracked PE wall is active or in initial interaction band — keep it.
                selected_wall = nearest_pe_wall
                wall_option_type = "PE"
            else:
                # Neither tracked wall qualifies for priority — pick whichever qualifying wall is nearest.
                if ce_dist <= pe_dist:
                    selected_wall = nearest_ce_wall
                    wall_option_type = "CE"
                else:
                    selected_wall = nearest_pe_wall
                    wall_option_type = "PE"
        elif nearest_ce_wall:
            selected_wall = nearest_ce_wall
            wall_option_type = "CE"
        elif nearest_pe_wall:
            selected_wall = nearest_pe_wall
            wall_option_type = "PE"

        if selected_wall is None:
            self.current_wall_key = None
            self.persistence_snapshots = 0
            self.first_seen = None
            self.last_seen = None
            self.has_interacted = False
            return None

        strike = float(selected_wall["strike"])
        wall_key = f"{wall_option_type}:{int(strike)}"
        direction = Direction.BEARISH if wall_option_type == "CE" else Direction.BULLISH
        trade_option_type = "PE" if direction == Direction.BEARISH else "CE"
        wall_oi = int(selected_wall["ce_oi"] if wall_option_type == "CE" else selected_wall["pe_oi"])
        wall_oi_change_pct = float(selected_wall["ce_oi_change_pct"] if wall_option_type == "CE" else selected_wall["pe_oi_change_pct"])

        # Relative percentile: 0..100 among non-zero same-side strikes
        same_side_ois = [
            row["ce_oi"] if wall_option_type == "CE" else row["pe_oi"]
            for row in full_chain
            if (row["ce_oi"] if wall_option_type == "CE" else row["pe_oi"]) > 0
        ]
        if same_side_ois:
            relative_percentile = round((sum(1 for oi in same_side_ois if oi <= wall_oi) / len(same_side_ois)) * 100.0, 1)
        else:
            relative_percentile = None

        # Persistence tracking
        breached = (spot > strike) if wall_option_type == "CE" else (spot < strike)

        if self.current_wall_key == wall_key:
            self.persistence_snapshots += 1
            self.last_seen = candle.timestamp
        else:
            self.current_wall_key = wall_key
            self.first_seen = candle.timestamp
            self.last_seen = candle.timestamp
            self.persistence_snapshots = 1
            self.has_interacted = False

        if self._check_candle_interaction(candle, strike, wall_option_type, interaction_dist):
            self.has_interacted = True

        persistence_duration = (self.last_seen - self.first_seen).total_seconds() if self.first_seen else 0.0
        req_snapshots = _setting_int("oi_wall_persistence_snapshots", 3)
        state = "PERSISTENT" if self.persistence_snapshots >= req_snapshots else "TRACKING"

        reasons = (
            f"Observed {wall_option_type} wall at {strike}",
            f"Wall size: {wall_oi / 100000.0:.1f}L contracts (+{wall_oi_change_pct:.1f}%)",
            f"Persistence: {self.persistence_snapshots} snapshot(s)",
        )

        bias = OIWallBias(
            wall_key=wall_key,
            wall_strike=strike,
            wall_option_type=wall_option_type,
            direction=direction,
            trade_option_type=trade_option_type,
            wall_oi=wall_oi,
            wall_oi_change_pct=wall_oi_change_pct,
            relative_percentile=relative_percentile,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            persistence_snapshots=self.persistence_snapshots,
            persistence_duration_seconds=persistence_duration,
            state=state,
            initial_interaction_timestamp=None,
            initial_interaction_price=None,
            favourable_excursion_pts=0.0,
            reasons=reasons,
        )

        if breached:
            self.current_wall_key = None
            self.persistence_snapshots = 0
            self.first_seen = None
            self.last_seen = None
            self.has_interacted = False

        return bias

    def _evaluate_confidence(
        self,
        candle: OHLCVCandle,
        bias: OIWallBias,
        levels: List[ResistanceLevel],
    ) -> Tuple[str, List[str]]:
        strike = bias.wall_strike
        extra_reasons = []

        conviction_mult = _setting_float("oi_wall_conviction_multiplier", 1.5)
        min_oi = _setting_float("oi_wall_min_oi", 4000000.0)
        min_oi_change = _setting_float("oi_wall_min_oi_change_pct", 5.0)

        # 1. Wall Magnitude
        oi_val = bias.wall_oi
        if oi_val >= conviction_mult * min_oi:
            extra_reasons.append(
                f"Massive wall size confirms strong barrier "
                f"(>{conviction_mult}x min)"
            )
            mag_score = 1
        else:
            mag_score = 0

        # 2. Active Defence (OI Growth)
        oi_change_pct = bias.wall_oi_change_pct
        if oi_change_pct >= conviction_mult * min_oi_change:
            extra_reasons.append(
                f"Aggressive active defending by option writers "
                f"(>{conviction_mult}x min change)"
            )
            growth_score = 1
        else:
            growth_score = 0

        # 3. Exact Level Penetration
        if bias.direction == Direction.BEARISH:
            pierced = candle.high >= strike
        else:
            pierced = candle.low <= strike

        if pierced:
            extra_reasons.append("Price tested wall deeply / pierced the strike")
            pierce_score = 1
        else:
            pierce_score = 0

        # 4. Wick Rejection
        candle_range = candle.high - candle.low
        wick_score = 0
        wick_min_range = _setting_float("oi_wall_wick_min_range_pts", 2.0)
        wick_rejection_ratio = _setting_float("oi_wall_wick_rejection_ratio", 0.4)
        if candle_range > wick_min_range:
            if bias.direction == Direction.BEARISH:
                if (candle.high - max(candle.open, candle.close)) >= wick_rejection_ratio * candle_range:
                    extra_reasons.append("Candle showed heavy overhead supply (long upper wick)")
                    wick_score = 1
            else:
                if (min(candle.open, candle.close) - candle.low) >= wick_rejection_ratio * candle_range:
                    extra_reasons.append("Candle showed strong absorption/buying tail (long lower wick)")
                    wick_score = 1

        score = mag_score + growth_score + pierce_score + wick_score
        confidence = confidence_from_score(score, max_score=4)
        return confidence, extra_reasons

    def build_signal(
        self,
        decision: OIWallEntryDecision,
        candle: OHLCVCandle,
        spot: float,
        levels: List[ResistanceLevel],
    ) -> AresSignal:
        """
        Constructs the AresSignal from an approved QUALIFIED entry decision.
        """
        if decision.status != "QUALIFIED" or decision.bias is None:
            raise ValueError("Cannot build signal from non-QUALIFIED decision")

        bias = decision.bias
        strike = bias.wall_strike
        direction = bias.direction
        option_type = bias.trade_option_type

        oi_lakhs = bias.wall_oi / 100000.0
        reasons = [
            f"Price approached massive OI wall at {strike}",
            f"Wall size: {oi_lakhs:.1f}L contracts (+{bias.wall_oi_change_pct:.1f}% change)",
            f"Confirmed {bias.persistence_snapshots} snapshot persistence",
            "Secondary pullback re-test confirmed holding defended side",
        ]

        confidence, extra_reasons = self._evaluate_confidence(candle, bias, levels)
        reasons.extend(extra_reasons)

        entry_zone_offset = _setting_float("entry_zone_offset_pts", 5.0)
        strike_interval = _setting_int("strike_interval", 50)

        trigger_price = decision.trigger_price if decision.trigger_price is not None else candle.close
        entry_zone = (trigger_price - entry_zone_offset, trigger_price + entry_zone_offset)
        strike_to_trade = int(round(spot / strike_interval) * strike_interval)

        return AresSignal(
            setup_type=SetupType.OI_WALL_REJECTION,
            direction=direction,
            trigger_price=trigger_price,
            entry_zone=entry_zone,
            stop_loss=0.0,      # set by engine.apply_per_type_levels
            target_1=0.0,       # set by engine.apply_per_type_levels
            target_2=0.0,       # set by engine.apply_per_type_levels
            confidence=confidence,
            reasons=reasons,
            timestamp=candle.timestamp,
            strike_to_trade=strike_to_trade,
            option_type=option_type,
            oi_wall_context=decision.telemetry.to_dict(),
        )

    def _build_signal(
        self,
        candle: OHLCVCandle,
        spot: float,
        wall: Dict[str, Any],
        direction: Direction,
        option_type: str,
        levels: List[ResistanceLevel],
    ) -> AresSignal:
        """
        Backwards-compatible helper for legacy test harnesses.
        """
        strike = float(wall["strike"])
        wall_oi = int(wall["ce_oi"] if direction == Direction.BEARISH else wall["pe_oi"])
        wall_oi_change_pct = float(wall.get("ce_oi_change_pct" if direction == Direction.BEARISH else "pe_oi_change_pct", 0.0))
        bias = OIWallBias(
            wall_key=f"{'CE' if direction == Direction.BEARISH else 'PE'}:{int(strike)}",
            wall_strike=strike,
            wall_option_type="CE" if direction == Direction.BEARISH else "PE",
            direction=direction,
            trade_option_type=option_type,
            wall_oi=wall_oi,
            wall_oi_change_pct=wall_oi_change_pct,
            relative_percentile=None,
            first_seen=candle.timestamp,
            last_seen=candle.timestamp,
            persistence_snapshots=1,
            persistence_duration_seconds=0.0,
            state="TRACKING",
            initial_interaction_timestamp=None,
            initial_interaction_price=None,
            favourable_excursion_pts=0.0,
            reasons=(),
        )
        decision = OIWallEntryDecision(
            status="QUALIFIED",
            wall_key=bias.wall_key,
            decision_id=f"{bias.wall_key}:test",
            bias=bias,
            telemetry=OIWallTelemetry(
                bias=bias,
                entry_status="QUALIFIED",
                filter_state="QUALIFIED",
                rejection_reason=None,
                initial_interaction_timestamp=None,
                initial_interaction_price=None,
                favourable_excursion_pts=None,
                retest_timestamp=candle.timestamp,
                reference_price=strike,
                vwap=None,
                opening_range=None,
            ),
            trigger_price=candle.close,
            retest_timestamp=candle.timestamp,
            rejection_reason=None,
            reference_price=strike,
        )
        return self.build_signal(decision, candle, spot, levels)
