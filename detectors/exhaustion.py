from collections import deque
from statistics import mean
from typing import Optional, List

from models import OHLCVCandle, AresSignal, SetupType, Direction, ResistanceLevel, confidence_from_score
from config import settings


class ExhaustionDetector:
    """
    ExhaustionDetector identifies "Exhaustion Reversal" setups.
    
    This pattern occurs when there is a volume climax (huge volume spike) combined
    with an indecision candle (doji-like) at an extreme price level. It signifies
    absorption by larger players and an impending reversal.
    
    The detector maintains a rolling history of volume to establish a dynamic baseline.
    """

    def __init__(self):
        """
        Initialize the volume history deque (last N periods, from
        exhaustion_volume_history_size) to compute a moving average of volume.
        The size is read at construction, so detectors must be created after
        the config profile is applied (main.py already does this).
        """
        self.volume_history = deque(maxlen=settings.exhaustion_volume_history_size)

    def update(self, candle: OHLCVCandle, iv_current: float, iv_prev: float, levels: List[ResistanceLevel]) -> Optional[AresSignal]:
        """
        Process the latest candle to determine if an exhaustion reversal pattern has formed.
        
        Args:
            candle: The latest closed 1-minute OHLCV candle.
            iv_current: The current ATM Implied Volatility.
            iv_prev: The previous cycle's ATM Implied Volatility.
            levels: Current key structural support and resistance levels.
            
        Returns:
            An AresSignal if the exhaustion pattern is detected, otherwise None.
        """
        self.volume_history.append(candle.volume)
        
        # Need enough history to establish a reliable average volume
        if len(self.volume_history) < settings.exhaustion_min_candles:
            return None
            
        avg_vol = mean(self.volume_history)
        
        # 1. Volume Climax
        volume_climax = candle.volume > (avg_vol * settings.exhaustion_volume_multiplier)
        
        # 2. Doji-like indecision candle
        body = abs(candle.close - candle.open)
        candle_range = candle.high - candle.low
        
        if candle_range == 0:
            doji_like = False
        else:
            doji_like = (body / candle_range) < settings.exhaustion_body_ratio
            
        # 3. IV Spike (optional confirmation condition)
        iv_spiked = (iv_current - iv_prev) > settings.exhaustion_iv_spike_threshold
        
        if volume_climax and doji_like:
            # Determine direction
            if candle.close > candle.open:
                direction = Direction.BEARISH
            else:
                direction = Direction.BULLISH
                
            return self._build_signal(
                candle=candle,
                direction=direction,
                avg_vol=avg_vol,
                iv_spiked=iv_spiked,
                levels=levels
            )
            
        return None

    def _build_signal(
        self,
        candle: OHLCVCandle,
        direction: Direction,
        avg_vol: float,
        iv_spiked: bool,
        levels: List[ResistanceLevel]
    ) -> AresSignal:
        """
        Constructs the AresSignal for an Exhaustion Reversal.
        
        This method handles:
        1. Contextual reason generation including volume climax and timestamps.
        2. Dynamic Target selection based on structural support/resistance.
        3. Fallback to fixed point targets if structural levels are too tight or missing.
        4. Deterministic sorting to ensure Target 1 (T1) is always the closer target,
           which is critical for the Position Manager's trailing stop logic.
        """
        vol_ratio = candle.volume / avg_vol if avg_vol > 0 else 0
        
        # 1. Extreme Volume Climax (factor over the base threshold)
        extreme_volume = candle.volume >= settings.exhaustion_extreme_volume_factor * avg_vol * settings.exhaustion_volume_multiplier

        # 2. Extreme Doji Body Ratio (factor under the base threshold)
        body = abs(candle.close - candle.open)
        candle_range = candle.high - candle.low
        extreme_doji = candle_range > 0 and (body / candle_range) <= settings.exhaustion_extreme_doji_factor * settings.exhaustion_body_ratio
        
        # 3. IV Climax
        iv_panic = iv_spiked
        
        # 4. Structural Level Match (within exhaustion_level_proximity_pts)
        near_level = False
        for lvl in levels:
            if direction == Direction.BEARISH:
                if abs(lvl.price - candle.high) <= settings.exhaustion_level_proximity_pts:
                    near_level = True
                    break
            else:
                if abs(lvl.price - candle.low) <= settings.exhaustion_level_proximity_pts:
                    near_level = True
                    break
                    
        score = sum([extreme_volume, extreme_doji, iv_panic, near_level])
        confidence = confidence_from_score(score, max_score=4)
        
        time_str = candle.timestamp.strftime("%I:%M%p").lower()
        # Remove leading zero from hour if present (e.g., 09:15am -> 9:15am)
        if time_str.startswith("0"):
            time_str = time_str[1:]
            
        reasons = [
            f"Volume climax detected ({vol_ratio:.1f}x average volume)",
            f"Candle formed a doji-like indecision pattern at {time_str}"
        ]
        
        if extreme_volume:
            reasons.append("Extreme climactic volume spike confirms high selling/buying pressure")
        if extreme_doji:
            reasons.append("Extreme doji-like body ratio confirms high price indecision")
        if iv_spiked:
            reasons.append("Sudden spike in Implied Volatility (IV) confirmed panic/exhaustion")
        if near_level:
            reasons.append("Exhaustion occurred at a significant key structural level")
            
        # SL / T1 / T2 are assigned centrally by the engine per setup type
        # (TASK-185, apply_per_type_levels). Detectors only classify direction.
        option_type = "PE" if direction == Direction.BEARISH else "CE"

        entry_zone = (candle.close - settings.entry_zone_offset_pts, candle.close + settings.entry_zone_offset_pts)
        strike_to_trade = int(round(candle.close / settings.strike_interval) * settings.strike_interval)
 
        return AresSignal(
            setup_type=SetupType.EXHAUSTION_REVERSAL,
            direction=direction,
            trigger_price=candle.close,
            entry_zone=entry_zone,
            stop_loss=0.0,      # set by engine.apply_per_type_levels (TASK-185)
            target_1=0.0,       # set by engine.apply_per_type_levels
            target_2=0.0,       # set by engine.apply_per_type_levels
            confidence=confidence,
            reasons=reasons,
            timestamp=candle.timestamp,
            strike_to_trade=strike_to_trade,
            option_type=option_type
        )
