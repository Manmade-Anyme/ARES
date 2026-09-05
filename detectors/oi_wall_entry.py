from datetime import datetime
from typing import Optional, List, Dict, Any, Set
from models import OHLCVCandle, ResistanceLevel, Direction, OIWallBias, OIWallTelemetry, OIWallEntryDecision
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


class OIWallEntryFilter:
    """
    Deterministic state machine for secondary re-test entry qualification.

    Transitions:
    NO_WALL -> TRACKING -> PERSISTENT -> INTERACTED -> RETEST_READY -> QUALIFIED -> CONSUMED
    Any breach / invalidation -> EXPIRED
    """

    def __init__(self):
        self.state: str = "NO_WALL"
        self.current_wall_key: Optional[str] = None
        self.consumed_wall_keys: Set[str] = set()
        self.initial_interaction_timestamp: Optional[datetime] = None
        self.initial_interaction_price: Optional[float] = None
        self.favourable_excursion_pts: float = 0.0
        self.retest_timestamp: Optional[datetime] = None
        self.retest_ready_timestamp: Optional[datetime] = None
        self.rejection_reason: Optional[str] = None
        self._latest_bias: Optional[OIWallBias] = None
        self.latest_watchlist_event: Optional[OIWallBias] = None
        self._watchlist_emitted_keys: Set[str] = set()

    def _reset_candidate(self) -> None:
        self.initial_interaction_timestamp = None
        self.initial_interaction_price = None
        self.favourable_excursion_pts = 0.0
        self.retest_timestamp = None
        self.retest_ready_timestamp = None
        self.rejection_reason = None
        self.latest_watchlist_event = None
        self.state = "NO_WALL"

    def _build_telemetry(
        self,
        bias: Optional[OIWallBias],
        entry_status: str,
        filter_state: str,
        candle: OHLCVCandle,
        rejection_reason: Optional[str] = None,
    ) -> OIWallTelemetry:
        raw_vwap = getattr(candle, "vwap", None)
        vwap = float(raw_vwap) if isinstance(raw_vwap, (int, float)) and raw_vwap > 0 else None
        return OIWallTelemetry(
            bias=bias,
            entry_status=entry_status,
            filter_state=filter_state,
            rejection_reason=rejection_reason,
            initial_interaction_timestamp=self.initial_interaction_timestamp,
            initial_interaction_price=self.initial_interaction_price,
            favourable_excursion_pts=self.favourable_excursion_pts if self.initial_interaction_timestamp else None,
            retest_timestamp=self.retest_timestamp,
            reference_price=bias.wall_strike if bias else None,
            vwap=vwap,
            opening_range=None,
        )

    def update(
        self,
        bias: Optional[OIWallBias],
        candle: OHLCVCandle,
        levels: List[ResistanceLevel],
    ) -> OIWallEntryDecision:
        self.latest_watchlist_event = None

        if bias is None:
            if self.state not in ("NO_WALL", "CONSUMED", "EXPIRED") and self._latest_bias:
                previous_bias = self._latest_bias
                self.state = "EXPIRED"
                self.rejection_reason = "Wall disappeared or no longer qualifies"
                telemetry = self._build_telemetry(
                    previous_bias,
                    entry_status="EXPIRED",
                    filter_state="EXPIRED",
                    candle=candle,
                    rejection_reason=self.rejection_reason,
                )
                self.current_wall_key = None
                self._latest_bias = None
                return OIWallEntryDecision(
                    status="EXPIRED",
                    wall_key=previous_bias.wall_key,
                    decision_id=None,
                    bias=previous_bias,
                    telemetry=telemetry,
                    trigger_price=None,
                    retest_timestamp=None,
                    rejection_reason=self.rejection_reason,
                    reference_price=previous_bias.wall_strike,
                )

            self._reset_candidate()
            self.current_wall_key = None
            self._latest_bias = None
            telemetry = self._build_telemetry(None, entry_status="NO_WALL", filter_state="NO_WALL", candle=candle)
            return OIWallEntryDecision(
                status="NO_WALL",
                wall_key=None,
                decision_id=None,
                bias=None,
                telemetry=telemetry,
                trigger_price=None,
                retest_timestamp=None,
                rejection_reason=None,
                reference_price=None,
            )

        self._latest_bias = bias

        # Check if wall has already been consumed in this session
        if bias.wall_key in self.consumed_wall_keys:
            self.state = "CONSUMED"
            reason = "Wall already consumed in this session"
            telemetry = self._build_telemetry(bias, entry_status="CONSUMED", filter_state="CONSUMED", candle=candle, rejection_reason=reason)
            return OIWallEntryDecision(
                status="CONSUMED",
                wall_key=bias.wall_key,
                decision_id=None,
                bias=bias,
                telemetry=telemetry,
                trigger_price=None,
                retest_timestamp=self.retest_timestamp,
                rejection_reason=reason,
                reference_price=bias.wall_strike,
            )

        # If wall identity changed, reset tracking for new wall
        if self.current_wall_key != bias.wall_key:
            self._reset_candidate()
            self.current_wall_key = bias.wall_key
            self.state = "TRACKING"

        strike = bias.wall_strike
        is_bearish = (bias.direction == Direction.BEARISH)  # CE wall above spot

        # Invalidation check: wrong side close (breach)
        breached = (candle.close > strike) if is_bearish else (candle.close < strike)
        if breached:
            self.state = "EXPIRED"
            self.rejection_reason = "Price closed beyond wall strike (breach)"
            telemetry = self._build_telemetry(bias, entry_status="EXPIRED", filter_state="EXPIRED", candle=candle, rejection_reason=self.rejection_reason)
            return OIWallEntryDecision(
                status="EXPIRED",
                wall_key=bias.wall_key,
                decision_id=None,
                bias=bias,
                telemetry=telemetry,
                trigger_price=None,
                retest_timestamp=None,
                rejection_reason=self.rejection_reason,
                reference_price=strike,
            )

        # Track initial interaction (can happen on snapshot 1, 2, or later)
        interaction_dist = _setting_float("oi_wall_initial_interaction_distance_pts", 20.0)
        req_persistence = _setting_int("oi_wall_persistence_snapshots", 3)
        min_excursion = _setting_float("oi_wall_min_excursion_pts", 12.0)
        retest_dist = _setting_float("oi_wall_retest_distance_pts", 20.0)

        if self.initial_interaction_timestamp is None:
            interacted = (
                (candle.high >= strike - interaction_dist)
                if is_bearish
                else (candle.low <= strike + interaction_dist)
            )
            defended = (candle.close <= strike) if is_bearish else (candle.close >= strike)
            if interacted and defended:
                self.initial_interaction_timestamp = candle.timestamp
                self.initial_interaction_price = candle.close
                self.state = "INTERACTED"
            else:
                if bias.persistence_snapshots >= req_persistence:
                    self.state = "PERSISTENT"
                else:
                    self.state = "TRACKING"

        if self.initial_interaction_timestamp is not None:
            # Excursion must be measured on a later candle
            if candle.timestamp > self.initial_interaction_timestamp:
                current_excursion = (strike - candle.low) if is_bearish else (candle.high - strike)
                if current_excursion > self.favourable_excursion_pts:
                    self.favourable_excursion_pts = current_excursion

            # Check if persistence and excursion allow RETEST_READY
            persistence_met = bias.persistence_snapshots >= req_persistence
            excursion_met = self.favourable_excursion_pts >= min_excursion

            if self.state in ("INTERACTED", "PERSISTENT") and persistence_met and excursion_met:
                self.state = "RETEST_READY"
                self.retest_ready_timestamp = candle.timestamp
                if bias.wall_key not in self._watchlist_emitted_keys:
                    self.latest_watchlist_event = bias
                    self._watchlist_emitted_keys.add(bias.wall_key)

            # In RETEST_READY, check for secondary re-test
            if (
                self.state == "RETEST_READY"
                and self.retest_ready_timestamp is not None
                and candle.timestamp > self.retest_ready_timestamp
            ):
                retested = (
                    (candle.high >= strike - retest_dist)
                    if is_bearish
                    else (candle.low <= strike + retest_dist)
                )
                if retested:
                    defended_retest = (candle.close <= strike) if is_bearish else (candle.close >= strike)
                    if defended_retest:
                        decision_id = f"{bias.wall_key}:{int(candle.timestamp.timestamp())}"
                        self.retest_timestamp = candle.timestamp
                        self.state = "QUALIFIED"
                        telemetry = self._build_telemetry(bias, entry_status="QUALIFIED", filter_state="QUALIFIED", candle=candle)
                        return OIWallEntryDecision(
                            status="QUALIFIED",
                            wall_key=bias.wall_key,
                            decision_id=decision_id,
                            bias=bias,
                            telemetry=telemetry,
                            trigger_price=candle.close,
                            retest_timestamp=candle.timestamp,
                            rejection_reason=None,
                            reference_price=strike,
                        )
                    else:
                        self.state = "EXPIRED"
                        self.rejection_reason = "Price closed beyond wall during re-test"
                        telemetry = self._build_telemetry(bias, entry_status="EXPIRED", filter_state="EXPIRED", candle=candle, rejection_reason=self.rejection_reason)
                        return OIWallEntryDecision(
                            status="EXPIRED",
                            wall_key=bias.wall_key,
                            decision_id=None,
                            bias=bias,
                            telemetry=telemetry,
                            trigger_price=None,
                            retest_timestamp=None,
                            rejection_reason=self.rejection_reason,
                            reference_price=strike,
                        )

        # Still waiting / tracking
        telemetry = self._build_telemetry(bias, entry_status="WAITING", filter_state=self.state, candle=candle)
        return OIWallEntryDecision(
            status="WAITING",
            wall_key=bias.wall_key,
            decision_id=None,
            bias=bias,
            telemetry=telemetry,
            trigger_price=None,
            retest_timestamp=None,
            rejection_reason=None,
            reference_price=strike,
        )

    def acknowledge(
        self,
        decision: OIWallEntryDecision,
        outcome: str,
    ) -> OIWallEntryDecision:
        candle_vwap = decision.telemetry.vwap if decision.telemetry else None

        if outcome == "EMITTED":
            self.state = "CONSUMED"
            if decision.wall_key:
                self.consumed_wall_keys.add(decision.wall_key)
            telemetry = OIWallTelemetry(
                bias=decision.bias,
                entry_status="CONSUMED",
                filter_state="CONSUMED",
                rejection_reason=None,
                initial_interaction_timestamp=self.initial_interaction_timestamp,
                initial_interaction_price=self.initial_interaction_price,
                favourable_excursion_pts=self.favourable_excursion_pts,
                retest_timestamp=self.retest_timestamp,
                reference_price=decision.reference_price,
                vwap=candle_vwap,
                opening_range=None,
            )
            return OIWallEntryDecision(
                status="CONSUMED",
                wall_key=decision.wall_key,
                decision_id=decision.decision_id,
                bias=decision.bias,
                telemetry=telemetry,
                trigger_price=decision.trigger_price,
                retest_timestamp=decision.retest_timestamp,
                rejection_reason=None,
                reference_price=decision.reference_price,
            )

        elif outcome in ("SUPPRESSED_BY_COOLDOWN", "SUPPRESSED_BY_PRIORITY"):
            self.state = "RETEST_READY"
            self.retest_timestamp = None
            self.retest_ready_timestamp = decision.retest_timestamp
            telemetry = OIWallTelemetry(
                bias=decision.bias,
                entry_status="WAITING",
                filter_state="RETEST_READY",
                rejection_reason=outcome,
                initial_interaction_timestamp=self.initial_interaction_timestamp,
                initial_interaction_price=self.initial_interaction_price,
                favourable_excursion_pts=self.favourable_excursion_pts,
                retest_timestamp=None,
                reference_price=decision.reference_price,
                vwap=candle_vwap,
                opening_range=None,
            )
            return OIWallEntryDecision(
                status="WAITING",
                wall_key=decision.wall_key,
                decision_id=None,
                bias=decision.bias,
                telemetry=telemetry,
                trigger_price=None,
                retest_timestamp=None,
                rejection_reason=outcome,
                reference_price=decision.reference_price,
            )

        elif outcome == "REJECTED_BY_RR":
            self.state = "EXPIRED"
            self.rejection_reason = "Rejected by R:R gate"
            telemetry = OIWallTelemetry(
                bias=decision.bias,
                entry_status="EXPIRED",
                filter_state="EXPIRED",
                rejection_reason=self.rejection_reason,
                initial_interaction_timestamp=self.initial_interaction_timestamp,
                initial_interaction_price=self.initial_interaction_price,
                favourable_excursion_pts=self.favourable_excursion_pts,
                retest_timestamp=None,
                reference_price=decision.reference_price,
                vwap=candle_vwap,
                opening_range=None,
            )
            return OIWallEntryDecision(
                status="EXPIRED",
                wall_key=decision.wall_key,
                decision_id=None,
                bias=decision.bias,
                telemetry=telemetry,
                trigger_price=None,
                retest_timestamp=None,
                rejection_reason=self.rejection_reason,
                reference_price=decision.reference_price,
            )

        return decision
