from dataclasses import dataclass
from typing import Optional, List

from models import OHLCVCandle, ResistanceLevel, AresSignal, SetupType, Direction, confidence_from_score
from config import settings


@dataclass
class ContinuationState:
    """
    Tracks a single trend-continuation candidate through its state machine:
    regime persistence -> armed -> pullback -> resumption.
    """
    direction: Direction
    regime_candles: int = 0
    armed: bool = False
    swing_extreme: Optional[float] = None
    in_pullback: bool = False
    pullback_candles: int = 0
    pullback_extreme: Optional[float] = None
    resume_pending: bool = False


class TrendContinuationDetector:
    """
    TrendContinuationDetector identifies "Trend Continuation" setups — the
    one trend-aligned entry in the ARES suite (TASK-177).

    Every other detector fades the move (failed breakout, OI wall rejection,
    exhaustion reversal); in a persistently trending session they can only
    produce counter-trend candidates, which the engine's trend filter
    correctly blocks. This detector instead requires the regime (the same
    VWAP + PDH/PDL rule as engine Filter E) to hold for a minimum run of
    candles, waits for a shallow pullback toward VWAP or a structural level,
    and enters once the trend resumes — so its signals are trend-aligned by
    construction and pass Filter E untouched.

    Resumption requires 2 consecutive trend-aligned candles (TASK-179), not
    just one: a single up-close (or down-close) inside an ongoing pullback
    is easy to mistake for the pullback ending when it's really just noise
    in an unfinished decline/rally — the entry then gets caught by the
    reversal continuing right after. Requiring the next candle to confirm
    filters that out at the cost of one candle's worth of entry price.
    """

    def __init__(self):
        """Initialize the detector with no active regime candidate."""
        self.state: Optional[ContinuationState] = None

    def update(
        self,
        candle: OHLCVCandle,
        avg_volume: float,
        levels: List[ResistanceLevel],
        pdh: Optional[float] = None,
        pdl: Optional[float] = None,
    ) -> Optional[AresSignal]:
        """
        Evaluate the latest candle against the regime/pullback/resumption
        state machine.

        Args:
            candle: The latest closed 1-minute OHLCV candle.
            avg_volume: A rolling average volume, for the resumption-volume
                scored condition.
            levels: Current key structural support/resistance levels.
            pdh: Previous day high. Required for the regime rule; the
                detector no-ops without it (mirrors engine Filter E).
            pdl: Previous day low. Required for the regime rule.

        Returns:
            An AresSignal if a trend-continuation setup resolves, otherwise None.
        """
        if pdh is None or pdl is None:
            return None

        is_uptrend = candle.close > candle.vwap and candle.close > pdl
        is_downtrend = candle.close < candle.vwap and candle.close < pdh

        if self.state is None:
            if is_uptrend:
                self.state = ContinuationState(direction=Direction.BULLISH, regime_candles=1)
            elif is_downtrend:
                self.state = ContinuationState(direction=Direction.BEARISH, regime_candles=1)
            return None

        state = self.state
        bull = state.direction == Direction.BULLISH

        if not state.armed:
            # Pre-arming, the candidate hasn't proven itself yet: hold it to
            # the strict regime rule. A vwap-side flip here is a genuine
            # failure to establish, not a pullback to tolerate.
            aligned = is_uptrend if bull else is_downtrend
            opposite = is_downtrend if bull else is_uptrend
            if opposite:
                if is_uptrend:
                    self.state = ContinuationState(direction=Direction.BULLISH, regime_candles=1)
                elif is_downtrend:
                    self.state = ContinuationState(direction=Direction.BEARISH, regime_candles=1)
                else:
                    self.state = None
                return None
            if aligned:
                state.regime_candles += 1
                if state.regime_candles >= settings.continuation_regime_min_candles:
                    state.armed = True
                    state.swing_extreme = candle.high if bull else candle.low
            # Flat (neither aligned nor opposite) candles before arming don't
            # advance the count but don't reset it either — brief pauses are
            # normal inside a building trend.
            return None

        # Once armed, a VWAP-side dip is the pullback we're waiting for, not
        # a regime break — only a genuine breach of the prior day's low/high
        # (a structural failure, not noise around the mean) aborts the
        # candidate. This is deliberately looser than the pre-arm rule.
        hard_break = (candle.close < pdl) if bull else (candle.close > pdh)
        if hard_break:
            self.state = None
            return None

        if not state.in_pullback:
            aligned = is_uptrend if bull else is_downtrend
            if aligned:
                state.regime_candles += 1
                if bull:
                    state.swing_extreme = max(state.swing_extreme, candle.high)
                else:
                    state.swing_extreme = min(state.swing_extreme, candle.low)

            near_vwap = abs(candle.close - candle.vwap) <= settings.continuation_pullback_vwap_pts
            near_level = any(
                abs(lvl.price - candle.close) <= settings.continuation_pullback_vwap_pts for lvl in levels
            )
            if near_vwap or near_level:
                state.in_pullback = True
                state.pullback_candles = 1
                state.pullback_extreme = candle.low if bull else candle.high
            return None

        # In pullback: track its depth, and watch for resumption or timeout.
        state.pullback_candles += 1
        if bull:
            state.pullback_extreme = min(state.pullback_extreme, candle.low)
        else:
            state.pullback_extreme = max(state.pullback_extreme, candle.high)

        if state.pullback_candles > settings.continuation_pullback_max_candles:
            self.state = None
            return None

        resumed = (
            (bull and candle.close > candle.open and candle.close > candle.vwap)
            or (not bull and candle.close < candle.open and candle.close < candle.vwap)
        )
        if not resumed:
            # A failed confirmation candle doesn't blow up the candidate --
            # just clears the pending flag so a later genuine 2-candle
            # confirm can still fire; the pullback itself keeps tracking.
            state.resume_pending = False
            return None

        if not state.resume_pending:
            # First trend-aligned candle only arms the confirmation gate
            # (TASK-179) -- proves nothing on its own yet.
            state.resume_pending = True
            return None

        signal = self._build_signal(candle=candle, state=state, avg_volume=avg_volume, levels=levels)
        self.state = None  # Reset after resolving, win or under-score
        return signal

    def _build_signal(
        self,
        candle: OHLCVCandle,
        state: ContinuationState,
        avg_volume: float,
        levels: List[ResistanceLevel],
    ) -> Optional[AresSignal]:
        """
        Scores the resumption candidate on 4 conditions and, if it clears
        continuation_min_score, builds the AresSignal: entry at the
        resumption close, SL at the exact pullback extreme (no buffer, per
        TASK-175), and structural targets shared with the other detectors.
        """
        direction = state.direction
        bull = direction == Direction.BULLISH

        # 1. Shallow pullback: price never broke back across VWAP during the retrace.
        shallow_pullback = (
            state.pullback_extreme > candle.vwap if bull else state.pullback_extreme < candle.vwap
        )
        # 2. Resumption volume: this candle confirms with above-average participation.
        resume_volume = avg_volume > 0 and candle.volume >= settings.continuation_resume_volume_ratio * avg_volume
        # 3. Strong regime: persisted well past the bare arming minimum.
        strong_regime = state.regime_candles >= 2 * settings.continuation_regime_min_candles
        # 4. Room to run: the nearest opposing structural level is far enough
        #    away to be worth the trade (reuses the shared target-selection bar).
        opposing = [lvl.price for lvl in levels if (lvl.price > candle.close if bull else lvl.price < candle.close)]
        room_to_run = (
            not opposing
            or min(abs(p - candle.close) for p in opposing) >= settings.structural_target_min_distance_pts
        )

        score = sum([shallow_pullback, resume_volume, strong_regime, room_to_run])
        if score < settings.continuation_min_score:
            return None

        confidence = confidence_from_score(score, max_score=4)

        reasons = [
            f"Trend continuation: regime held {state.regime_candles} candles before pullback",
            f"Pullback held {state.pullback_candles} candle(s), resumed at {candle.close:.2f}",
        ]
        if shallow_pullback:
            reasons.append("Pullback stayed on the trend side of VWAP — shallow retrace")
        if resume_volume:
            reasons.append(
                f"Resumption volume confirms participation "
                f"(>= {settings.continuation_resume_volume_ratio}x average)"
            )
        if strong_regime:
            reasons.append("Regime persisted well beyond the minimum arming window")
        if room_to_run:
            reasons.append("Sufficient room to the next opposing structural level")

        # --- Dynamic Target Selection (mirrors breakout/exhaustion detectors) ---
        target_1 = None
        target_2 = None

        if bull:
            option_type = "CE"
            # SL exactly at the pullback extreme — no buffer (TASK-175)
            stop_loss = state.pullback_extreme

            resistances = sorted([lvl.price for lvl in levels if lvl.price > candle.close])
            resistances = [r for r in resistances if abs(r - candle.close) >= settings.structural_target_min_distance_pts]

            if len(resistances) >= 1:
                target_1 = resistances[0]
                reasons.append(f"Target 1 set at structural resistance: {target_1:.2f}")
            if len(resistances) >= 2:
                target_2 = resistances[1]
                reasons.append(f"Target 2 set at structural resistance: {target_2:.2f}")

            if not target_1 or abs(target_1 - candle.close) < settings.target_1_fallback_min_pts:
                target_1 = candle.close + settings.target_1_pts
            if not target_2 or abs(target_2 - candle.close) < settings.target_2_fallback_min_pts:
                target_2 = candle.close + settings.target_2_pts
        else:
            option_type = "PE"
            # SL exactly at the pullback extreme — no buffer (TASK-175)
            stop_loss = state.pullback_extreme

            supports = sorted([lvl.price for lvl in levels if lvl.price < candle.close], reverse=True)
            supports = [s for s in supports if abs(s - candle.close) >= settings.structural_target_min_distance_pts]

            if len(supports) >= 1:
                target_1 = supports[0]
                reasons.append(f"Target 1 set at structural support: {target_1:.2f}")
            if len(supports) >= 2:
                target_2 = supports[1]
                reasons.append(f"Target 2 set at structural support: {target_2:.2f}")

            if not target_1 or abs(target_1 - candle.close) < settings.target_1_fallback_min_pts:
                target_1 = candle.close - settings.target_1_pts
            if not target_2 or abs(target_2 - candle.close) < settings.target_2_fallback_min_pts:
                target_2 = candle.close - settings.target_2_pts

        # Ensure correct ordering (T1 is closer to entry than T2)
        if bull and target_1 > target_2:
            target_1, target_2 = target_2, target_1
            reasons = [r.replace("Target 1", "TEMP").replace("Target 2", "Target 1").replace("TEMP", "Target 2") for r in reasons]
        elif not bull and target_1 < target_2:
            target_1, target_2 = target_2, target_1
            reasons = [r.replace("Target 1", "TEMP").replace("Target 2", "Target 1").replace("TEMP", "Target 2") for r in reasons]

        entry_zone = (candle.close - settings.entry_zone_offset_pts, candle.close + settings.entry_zone_offset_pts)
        strike_to_trade = int(round(candle.close / settings.strike_interval) * settings.strike_interval)

        return AresSignal(
            setup_type=SetupType.TREND_CONTINUATION,
            direction=direction,
            trigger_price=candle.close,
            entry_zone=entry_zone,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            confidence=confidence,
            reasons=reasons,
            timestamp=candle.timestamp,
            strike_to_trade=strike_to_trade,
            option_type=option_type,
        )
