from collections import deque
from statistics import mean
from typing import Optional, List

from models import OHLCVCandle, AresSignal, SetupType, Direction, ResistanceLevel
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
        Initialize the volume history deque. We keep the last 20 periods
        to compute a moving average of volume.
        """
        self.volume_history = deque(maxlen=20)

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
        
        time_str = candle.timestamp.strftime("%I:%M%p").lower()
        # Remove leading zero from hour if present (e.g., 09:15am -> 9:15am)
        if time_str.startswith("0"):
            time_str = time_str[1:]
            
        reasons = [
            f"Volume climax detected ({vol_ratio:.1f}x average volume)",
            f"Candle formed a doji-like indecision pattern at {time_str}"
        ]
        
        if iv_spiked:
            reasons.append("Sudden spike in Implied Volatility (IV) confirmed panic/exhaustion")
            
        # --- Dynamic Target Selection ---
        target_1 = None
        target_2 = None
        
        if direction == Direction.BEARISH:
            option_type = "PE"
            stop_loss = candle.high + settings.exhaustion_stop_buffer
            
            # Find supports below spot
            supports = sorted([lvl.price for lvl in levels if lvl.price < candle.close], reverse=True)
            if len(supports) >= 1:
                target_1 = supports[0]
                reasons.append(f"Target 1 set at structural support: {target_1:.2f}")
            if len(supports) >= 2:
                target_2 = supports[1]
                reasons.append(f"Target 2 set at structural support: {target_2:.2f}")
            
            # Fallback to fixed points if levels not found or too close
            if not target_1 or abs(target_1 - candle.close) < 15:
                target_1 = candle.close - settings.target_1_pts
            if not target_2 or abs(target_2 - candle.close) < 30:
                target_2 = candle.close - settings.target_2_pts
        else:
            option_type = "CE"
            stop_loss = candle.low - settings.exhaustion_stop_buffer
            
            # Find resistances above spot
            resistances = sorted([lvl.price for lvl in levels if lvl.price > candle.close])
            if len(resistances) >= 1:
                target_1 = resistances[0]
                reasons.append(f"Target 1 set at structural resistance: {target_1:.2f}")
            if len(resistances) >= 2:
                target_2 = resistances[1]
                reasons.append(f"Target 2 set at structural resistance: {target_2:.2f}")
                
            if not target_1 or abs(target_1 - candle.close) < 15:
                target_1 = candle.close + settings.target_1_pts
            if not target_2 or abs(target_2 - candle.close) < 30:
                target_2 = candle.close + settings.target_2_pts

        # Ensure correct ordering (T1 is closer to entry than T2)
        if direction == Direction.BEARISH and target_1 < target_2:
            target_1, target_2 = target_2, target_1
            reasons = [r.replace("Target 1", "TEMP").replace("Target 2", "Target 1").replace("TEMP", "Target 2") for r in reasons]
        elif direction == Direction.BULLISH and target_1 > target_2:
            target_1, target_2 = target_2, target_1
            reasons = [r.replace("Target 1", "TEMP").replace("Target 2", "Target 1").replace("TEMP", "Target 2") for r in reasons]

        entry_zone = (candle.close - settings.entry_zone_offset_pts, candle.close + settings.entry_zone_offset_pts)
        strike_to_trade = int(round(candle.close / settings.strike_interval) * settings.strike_interval)

        return AresSignal(
            setup_type=SetupType.EXHAUSTION_REVERSAL,
            direction=direction,
            trigger_price=candle.close,
            entry_zone=entry_zone,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            confidence="MEDIUM",  # Exhaustion setups are inherently trickier to time perfectly
            reasons=reasons,
            timestamp=candle.timestamp,
            strike_to_trade=strike_to_trade,
            option_type=option_type
        )
