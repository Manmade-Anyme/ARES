from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Tuple

from models import OHLCVCandle, ResistanceLevel, AresSignal, SetupType, Direction, confidence_from_score
from config import settings


@dataclass
class BreakoutState:
    """
    Holds the state of an active breakout event that is currently being monitored
    for potential failure.
    """
    level: float
    source: str
    breakout_candle: OHLCVCandle
    breakout_time: datetime
    candles_since: int = 0


class FailedBreakoutDetector:
    """
    FailedBreakoutDetector identifies "Failed Breakout" setups.
    
    A failed breakout occurs when the price forcefully crosses a significant support or
    resistance level, but then fails to hold that direction and closes back across the level.
    This detector tracks the active breakout and scores the failure based on volume,
    implied volatility (IV) crush, and whether option writers (OI) held their positions.
    
    Crucially, this detector is fully bidirectional, handling both upward breakouts
    (looking at CE OI) and downward breakdowns (looking at PE OI).
    """

    def __init__(self):
        """Initialize the detector with no active breakout."""
        self.active: Optional[BreakoutState] = None

    def update(
        self,
        candle: OHLCVCandle,
        avg_volume: float,
        iv_change_pct: float,
        atm_ce_oi: int,
        atm_ce_oi_prev: int,
        atm_pe_oi: int,
        atm_pe_oi_prev: int,
        levels: List[ResistanceLevel]
    ) -> Optional[AresSignal]:
        """
        Evaluate the latest candle against the support/resistance levels to detect
        or monitor a breakout state.
        
        Args:
            candle: The latest closed 1-minute OHLCV candle.
            avg_volume: A rolling average volume to evaluate volume strength.
            iv_change_pct: The percentage change in ATM Implied Volatility.
            atm_ce_oi: Current Open Interest for the ATM Call option.
            atm_ce_oi_prev: Previous Open Interest for the ATM Call option.
            atm_pe_oi: Current Open Interest for the ATM Put option.
            atm_pe_oi_prev: Previous Open Interest for the ATM Put option.
            levels: Current key structural support and resistance levels.
            
        Returns:
            An AresSignal object if a high-confidence failed breakout is detected, otherwise None.
        """
        # Step 1: If no active breakout, scan for a new level cross
        if not self.active:
            for lvl in levels:
                # Crossed above resistance
                crossed_above = candle.open < lvl.price and candle.close > lvl.price
                # Crossed below support
                crossed_below = candle.open > lvl.price and candle.close < lvl.price
                
                if crossed_above or crossed_below:
                    self.active = BreakoutState(
                        level=lvl.price,
                        source=lvl.source,
                        breakout_candle=candle,
                        breakout_time=candle.timestamp
                    )
                    return None
            return None

        # Step 2: Monitor active breakout
        self.active.candles_since += 1
        
        if self.active.candles_since > settings.breakout_confirmation_candles:
            # Price held the breakout long enough; it's a real breakout, not a failure.
            self.active = None
            return None

        # Step 3: Score failure conditions
        was_upward = self.active.breakout_candle.close > self.active.level
        
        # Check bidirectional conditions appropriately
        if was_upward:
            closed_back = candle.close < self.active.level
            writers_holding = atm_ce_oi >= atm_ce_oi_prev
            direction = Direction.BEARISH
            oi_change = ((atm_ce_oi - atm_ce_oi_prev) / atm_ce_oi_prev * 100.0) if atm_ce_oi_prev > 0 else 0.0
            deep_close = (self.active.level - candle.close) >= settings.breakout_deep_close_pts
        else:
            closed_back = candle.close > self.active.level
            writers_holding = atm_pe_oi >= atm_pe_oi_prev
            direction = Direction.BULLISH
            oi_change = ((atm_pe_oi - atm_pe_oi_prev) / atm_pe_oi_prev * 100.0) if atm_pe_oi_prev > 0 else 0.0
            deep_close = (candle.close - self.active.level) >= settings.breakout_deep_close_pts

        weak_volume = self.active.breakout_candle.volume < (avg_volume * settings.breakout_weak_volume_ratio)
        iv_falling = iv_change_pct < settings.breakout_iv_falling_threshold
        writers_active = oi_change >= settings.breakout_writers_active_min_pct

        # Score is the sum of True conditions (scale of 0 to 4).
        # closed_back is deliberately NOT scored (TASK-172, audit item 8): it is
        # the mandatory gate below, so counting it inflated every failure by a
        # free point and let "closed back + one coin-flip" fire a signal.
        # writers_holding is also NOT scored (TASK-174): OI merely holding
        # (including zero change) is satisfied by noise, and growth past the
        # writers_active threshold implies holding anyway — scoring both awarded
        # two points for a single OI reading. It survives as a reason string.
        score = sum([weak_volume, iv_falling, writers_active, deep_close])

        # If it closed back and meets the minimum failure score, trigger the signal
        if closed_back and score >= settings.breakout_failure_min_score:
            signal = self._build_signal(
                candle=candle,
                level=self.active.level,
                direction=direction,
                score=score,
                weak_vol=weak_volume,
                iv_falling=iv_falling,
                writers_held=writers_holding,
                writers_active=writers_active,
                deep_close=deep_close,
                levels=levels
            )
            self.active = None  # Reset state after generating signal
            return signal
            
        return None

    def _build_signal(
        self,
        candle: OHLCVCandle,
        level: float,
        direction: Direction,
        score: int,
        weak_vol: bool,
        iv_falling: bool,
        writers_held: bool,
        writers_active: bool,
        deep_close: bool,
        levels: List[ResistanceLevel]
    ) -> AresSignal:
        """
        Constructs the final AresSignal based on the collected conditions.
        
        This method handles:
        1. Confidence scoring and reason string generation.
        2. Dynamic Target selection based on structural support/resistance.
        3. Fallback to fixed point targets if structural levels are too tight or missing.
        4. Deterministic sorting to ensure Target 1 (T1) is always the closer target,
           which is critical for the Position Manager's trailing stop logic.
        """
        confidence = confidence_from_score(score, max_score=4)

        # Dynamically build reasons
        reasons = [f"Price closed back past level {level}"]
        if weak_vol:
            reasons.append("Breakout candle volume was weak")
        if iv_falling:
            reasons.append("IV is dropping rapidly (IV Crush)")
        if writers_held:
            reasons.append("Option writers did not cover their positions")
        if writers_active:
            reasons.append(
                f"Option writers actively defended and added positions "
                f"(>={settings.breakout_writers_active_min_pct}% OI growth)"
            )
        if deep_close:
            reasons.append(
                f"Price closed back deeply past level "
                f"(>={settings.breakout_deep_close_pts} points)"
            )
            
        # --- Dynamic Target Selection ---
        target_1 = None
        target_2 = None
        
        if direction == Direction.BEARISH:
            option_type = "PE"
            # SL exactly at the failed level — no buffer (TASK-175)
            stop_loss = level

            # Find supports below spot
            supports = sorted([lvl.price for lvl in levels if lvl.price < candle.close], reverse=True)
            # Add minimum distance check
            supports = [s for s in supports if abs(s - candle.close) >= settings.structural_target_min_distance_pts]
            
            if len(supports) >= 1:
                target_1 = supports[0]
                reasons.append(f"Target 1 set at structural support: {target_1:.2f}")
            if len(supports) >= 2:
                target_2 = supports[1]
                reasons.append(f"Target 2 set at structural support: {target_2:.2f}")
            
            # Fallback to fixed points if levels not found or too close
            if not target_1 or abs(target_1 - candle.close) < settings.target_1_fallback_min_pts:
                target_1 = candle.close - settings.target_1_pts
            if not target_2 or abs(target_2 - candle.close) < settings.target_2_fallback_min_pts:
                target_2 = candle.close - settings.target_2_pts
        else:
            option_type = "CE"
            # SL exactly at the failed level — no buffer (TASK-175)
            stop_loss = level

            # Find resistances above spot
            resistances = sorted([lvl.price for lvl in levels if lvl.price > candle.close])
            # Add minimum distance check
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

        # Ensure correct ordering (T1 is closer to entry than T2)
        if direction == Direction.BEARISH and target_1 < target_2:
            target_1, target_2 = target_2, target_1
            reasons = [r.replace("Target 1", "TEMP").replace("Target 2", "Target 1").replace("TEMP", "Target 2") for r in reasons]
        elif direction == Direction.BULLISH and target_1 > target_2:
            target_1, target_2 = target_2, target_1
            reasons = [r.replace("Target 1", "TEMP").replace("Target 2", "Target 1").replace("TEMP", "Target 2") for r in reasons]

        # Calculate entry zone (+/- setting points around the close)
        entry_zone = (candle.close - settings.entry_zone_offset_pts, candle.close + settings.entry_zone_offset_pts)

        # Strike should be rounded to the nearest configured interval (e.g., 50)
        strike_to_trade = int(round(candle.close / settings.strike_interval) * settings.strike_interval)

        return AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
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
            option_type=option_type
        )
