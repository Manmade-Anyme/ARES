from collections import deque
from datetime import datetime, timedelta
from statistics import mean
from typing import Optional, List, Dict, Any

from models import OHLCVCandle, ATMStrikes, AresSignal, ResistanceLevel, Direction
from config import settings


def apply_per_type_levels(signal: AresSignal, settings) -> None:
    """Apply the per-setup-type SL/T1/T2 policy in place (TASK-185).

    SL and T1 are REPLACED with fixed absolute distances from the trigger price,
    looked up by setup type. T2 keeps the detector's structural value when it
    lies beyond the new T1; otherwise it falls back to the per-type distance.
    Unknown/unconfigured setup types are left untouched (structural behavior).
    """
    lv = settings.per_type_levels.get(signal.setup_type.value)
    if lv is None:
        return
    entry = signal.trigger_price
    sign = 1 if signal.direction == Direction.BULLISH else -1
    signal.stop_loss = entry - sign * lv.stop_pts
    signal.target_1 = entry + sign * lv.target_1_pts
    # Keep a structural T2 only if it sits beyond the new T1 in the trade's
    # favourable direction; else use the per-type fallback distance.
    if (signal.target_2 - signal.target_1) * sign <= 0:
        signal.target_2 = entry + sign * lv.target_2_fallback_pts

from detectors.breakout import FailedBreakoutDetector
from detectors.oi_wall import OIWallDetector
from detectors.exhaustion import ExhaustionDetector
from detectors.continuation import TrendContinuationDetector


class AresEngine:
    """
    AresEngine orchestrates the three core ARES detectors on every cycle tick.
    
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
        self.continuation_detector = TrendContinuationDetector()
        self.exhaustion_detector = ExhaustionDetector()
        
        self.candle_buffer: deque = deque(maxlen=settings.candle_buffer_size)
        self.iv_buffer: deque = deque(maxlen=settings.iv_buffer_size)

        self.last_signal_time: Optional[datetime] = None

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

        # 2. Cooldown check
        if self.last_signal_time:
            elapsed = datetime.now() - self.last_signal_time
            if elapsed < timedelta(minutes=settings.signal_cooldown_minutes):
                return None

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

        # 5. Run detectors in priority order
        # We use 'or' to short-circuit: if a higher-priority detector returns a signal,
        # the subsequent ones won't execute.
        signal = (
            self.breakout_detector.update(
                candle=candle,
                avg_volume=avg_volume,
                iv_change_pct=iv_change_pct,
                atm_ce_oi=atm.ce.oi,
                atm_ce_oi_prev=atm.ce.oi_prev,
                atm_pe_oi=atm.pe.oi,
                atm_pe_oi_prev=atm.pe.oi_prev,
                levels=levels
            )
            or
            self.oi_wall_detector.update(
                spot=atm.spot_price,
                full_chain=full_chain,
                candle=candle,
                levels=levels
            )
            or
            (
                self.continuation_detector.update(
                    candle=candle,
                    avg_volume=avg_volume,
                    levels=levels,
                    pdh=pdh,
                    pdl=pdl
                )
                if settings.continuation_enabled else None
            )
            or
            self.exhaustion_detector.update(
                candle=candle,
                iv_current=atm.ce.iv,
                iv_prev=iv_prev,
                levels=levels
            )
        )

        # 6. Apply the risk:reward gate — the only remaining protective filter.
        # The observation-only gate (exhaustion/continuation alert-only modes),
        # the trend-regime filter, the flat-market speed filter and the
        # anti-IV-crush filter were all removed (TASK-182): they silenced the
        # system in trending sessions by turning tradeable setups into
        # observation-only alerts or suppressing them outright. Every fired
        # setup is now a live trade unless its risk:reward is degenerate.
        # 5b. Per-setup-type SL/T1/T2 policy (TASK-185). Replaces structural SL
        # and T1 with fixed per-type distances and sets the T2 fallback, keyed
        # by setup type from the active profile. Runs BEFORE the R:R gate so the
        # gate evaluates the final, per-type levels.
        if signal:
            apply_per_type_levels(signal, settings)

        if signal:
            # Risk:Reward Gate (reject setups whose risk to SL exceeds reward to T1)
            risk = abs(signal.trigger_price - signal.stop_loss)
            reward = abs(signal.target_1 - signal.trigger_price)
            # Degenerate SL placement (zero/negative risk) is always rejected
            if risk <= 0 or (reward / risk) < settings.min_rr_ratio:
                rr = (reward / risk) if risk > 0 else 0.0
                print(f"[-] AresEngine: Suppressing {signal.setup_type.value} ({signal.direction.value}) signal. Reason: R:R {rr:.2f} below minimum {settings.min_rr_ratio:.2f} (risk {risk:.1f} pts vs reward {reward:.1f} pts).")
                signal = None

        # 6b. Flat-market annotation (TASK-182 follow-up). NOT a gate: if the
        # rolling window range is below the threshold, append an informational
        # reason so the alert flags a consolidating market — the signal still
        # trades. This is the old speed filter's condition, reused as a warning
        # instead of a suppressor.
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
