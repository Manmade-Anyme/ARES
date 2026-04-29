from collections import deque
from statistics import mean
from typing import Optional

from models import OHLCVCandle, AresSignal, SetupType, Direction
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

    def update(self, candle: OHLCVCandle, iv_current: float, iv_prev: float) -> Optional[AresSignal]:
        """
        Process the latest candle to determine if an exhaustion reversal pattern has formed.
        
        Args:
            candle: The latest closed 1-minute OHLCV candle.
            iv_current: The current ATM Implied Volatility.
            iv_prev: The previous cycle's ATM Implied Volatility.
            
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
            # A green doji (close > open) at the top of a move indicates the final push upwards -> Bearish Reversal
            # A red doji (close < open) at the bottom indicates the final push downwards -> Bullish Reversal
            if candle.close > candle.open:
                direction = Direction.BEARISH
            else:
                direction = Direction.BULLISH
                
            return self._build_signal(
                candle=candle,
                direction=direction,
                avg_vol=avg_vol,
                iv_spiked=iv_spiked
            )
            
        return None

    def _build_signal(
        self,
        candle: OHLCVCandle,
        direction: Direction,
        avg_vol: float,
        iv_spiked: bool
    ) -> AresSignal:
        """
        Constructs the AresSignal for an Exhaustion Reversal.
        """
        vol_ratio = candle.volume / avg_vol if avg_vol > 0 else 0
        
        reasons = [
            f"Volume climax detected ({vol_ratio:.1f}x average volume)",
            "Candle formed a doji-like indecision pattern"
        ]
        
        if iv_spiked:
            reasons.append("Sudden spike in Implied Volatility (IV) confirmed panic/exhaustion")
            
        if direction == Direction.BEARISH:
            option_type = "PE"
            # Stop above the high of the exhaustion candle
            stop_loss = candle.high + settings.exhaustion_stop_buffer
            target_1 = candle.close - settings.target_1_pts
            target_2 = candle.close - settings.target_2_pts
        else:
            option_type = "CE"
            # Stop below the low of the exhaustion candle
            stop_loss = candle.low - settings.exhaustion_stop_buffer
            target_1 = candle.close + settings.target_1_pts
            target_2 = candle.close + settings.target_2_pts

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
