from collections import deque
from datetime import datetime, timedelta
from statistics import mean
from typing import Optional, List, Dict, Any

from models import OHLCVCandle, ATMStrikes, AresSignal, ResistanceLevel, Direction, SetupType, OIWallBias
from config import settings


def apply_per_type_levels(signal: AresSignal, settings, levels=None) -> None:
    """Set the per-setup-type SL/T1/T2 for a signal in place (TASK-185).

    This is the single source of truth for a signal's trade levels — detectors
    no longer compute them. Keyed by setup type from the active profile:
      * SL and T1 are fixed absolute distances from the trigger price.
      * T2 is the nearest structural support/resistance (from ``levels``) that
        lies beyond T1 in the trade's favourable direction; when no such level
        exists it falls back to the per-type distance.
    Unknown/unconfigured setup types are left untouched (no per-type entry).
    """
    lv = settings.per_type_levels.get(signal.setup_type.value)
    if lv is None:
        return
    entry = signal.trigger_price
    sign = 1 if signal.direction == Direction.BULLISH else -1
    signal.stop_loss = entry - sign * lv.stop_pts
    signal.target_1 = entry + sign * lv.target_1_pts
    signal.target_2 = _resolve_target_2(entry, sign, signal.target_1, lv, levels)


def _resolve_target_2(entry, sign, target_1, lv, levels):
    """T2 = nearest structural level beyond T1 (favourable side), else per-type
    fallback distance from entry."""
    beyond = [lvl.price for lvl in (levels or []) if (lvl.price - target_1) * sign > 0]
    if beyond:
        return min(beyond, key=lambda p: (p - target_1) * sign)
    return entry + sign * lv.target_2_fallback_pts

from detectors.breakout import FailedBreakoutDetector
from detectors.oi_wall import OIWallDetector
from detectors.oi_wall_entry import OIWallEntryFilter
from detectors.exhaustion import ExhaustionDetector
from detectors.continuation import TrendContinuationDetector


class AresEngine:
    """
    AresEngine orchestrates the core ARES detectors on every cycle tick.
    
    It maintains rolling buffers for volume and IV, and strictly enforces a signal cooldown
    period to prevent spam during choppy market conditions.
    
    It operates completely standalone.
    """

    def __init__(self):
        """
        Initialize the detectors and rolling buffers.
        """
        self.breakout_detector = FailedBreakoutDetector()
        self.oi_wall_detector = OIWallDetector()
        self.oi_wall_filter = OIWallEntryFilter()
        self.continuation_detector = TrendContinuationDetector()
        self.exhaustion_detector = ExhaustionDetector()
        
        self.candle_buffer: deque = deque(maxlen=settings.candle_buffer_size)
        self.iv_buffer: deque = deque(maxlen=settings.iv_buffer_size)

        self.last_signal_time: Optional[datetime] = None
        self._latest_oi_wall_context: Optional[Dict[str, Any]] = None

    @property
    def latest_oi_wall_context(self) -> Optional[Dict[str, Any]]:
        """Latest serialized OI wall context for ML collector and audit."""
        return self._latest_oi_wall_context

    @property
    def latest_watchlist_event(self) -> Optional[OIWallBias]:
        """Latest watchlist event emitted when an OI wall becomes RETEST_READY."""
        return self.oi_wall_filter.latest_watchlist_event

    @property
    def latest_expired_decision(self) -> Optional[OIWallEntryDecision]:
        """Latest decision that has expired/invalidated."""
        return self.oi_wall_filter.latest_expired_decision

    def tick(
        self,
        candle: OHLCVCandle,
        full_chain: List[Dict[str, Any]],
        atm: ATMStrikes,
        iv_change_pct: float,
        levels: List[ResistanceLevel],
        pdh: Optional[float] = None,
        pdl: Optional[float] = None,
    ) -> Optional[AresSignal]:
        """
        Process a single tick of data through the detection pipeline.

        Priority Order Rationale:
        1. Failed Breakout (Highest Precision, stateful tracking)
        2. OI Wall Rejection (High Precision, structural support/resistance)
        3. Trend Continuation (trend-aligned, TASK-177)
        4. Exhaustion Reversal (Medium Precision, volume/price extreme)

        Higher confidence setups are checked first. If a signal is found, the
        evaluation short-circuits and returns.

        Args:
            candle: The latest closed OHLCV candle.
            full_chain: The complete NIFTY option chain from OIFetcher.
            atm: The ATM strikes context including spot price and ATM IV/OI.
            iv_change_pct: The percentage change in ATM Implied Volatility.
            levels: A list of ResistanceLevel objects (structural levels + OI walls) used for target calculation.
            pdh: Previous day high, used by the trend-continuation detector's
                regime rule. Optional — the detector no-ops without it.
            pdl: Previous day low, used by the trend-continuation detector's
                regime rule. Optional — the detector no-ops without it.

        Returns:
            An AresSignal if a detector triggers and cooldown is clear, otherwise None.
        """
        # 1. Update buffers
        self.candle_buffer.append(candle)
        self.iv_buffer.append(atm.ce.iv)

        # 2. Cooldown state. Evaluated here but applied *after* the stateful
        # detectors have advanced (TASK-188) — see step 5a.
        in_cooldown = bool(
            self.last_signal_time
            and (datetime.now() - self.last_signal_time)
            < timedelta(minutes=settings.signal_cooldown_minutes)
        )

        # 3. Calculate average volume
        if len(self.candle_buffer) > 0:
            avg_volume = mean([c.volume for c in self.candle_buffer])
        else:
            avg_volume = float(candle.volume)

        # 4. Get previous IV
        if len(self.iv_buffer) >= 2:
            iv_prev = self.iv_buffer[-2]
        else:
            iv_prev = self.iv_buffer[-1]

        # 5a. Advance the stateful OI wall detector and entry filter on EVERY candle (TASK-073).
        # It updates persistence, tracking, and qualification regardless of cooldown
        # or detector priority. Only signal emission is gated below.
        oi_wall_bias = self.oi_wall_detector.update(
            spot=atm.spot_price,
            full_chain=full_chain,
            candle=candle,
            levels=levels,
        )
        if isinstance(oi_wall_bias, OIWallBias):
            oi_wall_decision = self.oi_wall_filter.update(
                bias=oi_wall_bias,
                candle=candle,
                levels=levels,
            )
            self._latest_oi_wall_context = (
                oi_wall_decision.telemetry.to_dict()
                if oi_wall_decision.status != "NO_WALL"
                else None
            )

            oi_wall_candidate: Optional[AresSignal] = None
            if oi_wall_decision.status == "QUALIFIED":
                oi_wall_candidate = self.oi_wall_detector.build_signal(
                    decision=oi_wall_decision,
                    candle=candle,
                    spot=atm.spot_price,
                    levels=levels,
                )
        else:
            oi_wall_decision = self.oi_wall_filter.update(
                bias=None,
                candle=candle,
                levels=levels,
            )
            self._latest_oi_wall_context = (
                oi_wall_decision.telemetry.to_dict()
                if oi_wall_decision.status != "NO_WALL"
                else None
            )
            oi_wall_candidate = oi_wall_bias

        if in_cooldown:
            if oi_wall_decision.status == "QUALIFIED":
                ack = self.oi_wall_filter.acknowledge(oi_wall_decision, "SUPPRESSED_BY_COOLDOWN")
                self._latest_oi_wall_context = ack.telemetry.to_dict()
            return None

        # 5. Run detectors in priority order
        breakout_signal = self.breakout_detector.update(
            candle=candle,
            avg_volume=avg_volume,
            iv_change_pct=iv_change_pct,
            atm_ce_oi=atm.ce.oi,
            atm_ce_oi_prev=atm.ce.oi_prev,
            atm_pe_oi=atm.pe.oi,
            atm_pe_oi_prev=atm.pe.oi_prev,
            levels=levels,
        )

        if breakout_signal:
            if oi_wall_decision.status == "QUALIFIED":
                ack = self.oi_wall_filter.acknowledge(oi_wall_decision, "SUPPRESSED_BY_PRIORITY")
                self._latest_oi_wall_context = ack.telemetry.to_dict()
            signal = breakout_signal
        elif oi_wall_candidate:
            signal = oi_wall_candidate
        else:
            signal = (
                (
                    self.continuation_detector.update(
                        candle=candle,
                        avg_volume=avg_volume,
                        levels=levels,
                        pdh=pdh,
                        pdl=pdl,
                    )
                    if settings.continuation_enabled
                    else None
                )
                or self.exhaustion_detector.update(
                    candle=candle,
                    iv_current=atm.ce.iv,
                    iv_prev=iv_prev,
                    levels=levels,
                )
            )

        # 5b. Per-setup-type SL/T1/T2 policy (TASK-185).
        if signal:
            apply_per_type_levels(signal, settings, levels)

        if signal:
            # Risk:Reward Gate (reject setups whose risk to SL exceeds reward to T1)
            risk = abs(signal.trigger_price - signal.stop_loss)
            reward = abs(signal.target_1 - signal.trigger_price)
            # Degenerate SL placement (zero/negative risk) is always rejected
            if risk <= 0 or (reward / risk) < settings.min_rr_ratio:
                rr = (reward / risk) if risk > 0 else 0.0
                print(f"[-] AresEngine: Suppressing {signal.setup_type.value} ({signal.direction.value}) signal. Reason: R:R {rr:.2f} below minimum {settings.min_rr_ratio:.2f} (risk {risk:.1f} pts vs reward {reward:.1f} pts).")
                if signal.setup_type == SetupType.OI_WALL_REJECTION and oi_wall_decision.status == "QUALIFIED":
                    ack = self.oi_wall_filter.acknowledge(oi_wall_decision, "REJECTED_BY_RR")
                    self._latest_oi_wall_context = ack.telemetry.to_dict()
                signal = None

        # Acknowledge emission for OI wall setup
        if signal and signal.setup_type == SetupType.OI_WALL_REJECTION and oi_wall_decision.status == "QUALIFIED":
            ack = self.oi_wall_filter.acknowledge(oi_wall_decision, "EMITTED")
            self._latest_oi_wall_context = ack.telemetry.to_dict()
            signal.oi_wall_context = ack.telemetry.to_dict()

        # 6b. Flat-market annotation (TASK-182 follow-up).
        if signal:
            window = settings.speed_filter_window_candles
            if len(self.candle_buffer) >= window:
                recent = list(self.candle_buffer)[-window:]
                rolling_range = max(c.high for c in recent) - min(c.low for c in recent)
                if rolling_range < settings.speed_filter_min_range_pts:
                    signal.reasons.append(
                        f"Price is FLAT — market moving under "
                        f"{int(settings.speed_filter_min_range_pts)} points "
                        f"(last {window}-candle range: {rolling_range:.1f} pts)"
                    )

        # 7. Set cooldown if a signal fired.
        if signal:
            self.last_signal_time = datetime.now()

        # 8. Return the result
        return signal

    def clear_cooldown(self) -> None:
        """
        Reset the signal cooldown. Called after a trade stops out so a fresh
        setup can be taken immediately instead of waiting out the timer.
        """
        self.last_signal_time = None
