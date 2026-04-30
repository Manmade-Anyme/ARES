from collections import deque
from datetime import datetime, timedelta
from statistics import mean
from typing import Optional, List, Dict, Any

from models import OHLCVCandle, ATMStrikes, AresSignal, ResistanceLevel
from config import settings

from detectors.breakout import FailedBreakoutDetector
from detectors.oi_wall import OIWallDetector
from detectors.exhaustion import ExhaustionDetector


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
        levels: List[ResistanceLevel]
    ) -> Optional[AresSignal]:
        """
        Process a single tick of data through the detection pipeline.
        
        Priority Order Rationale:
        1. Failed Breakout (Highest Precision, stateful tracking)
        2. OI Wall Rejection (High Precision, structural support/resistance)
        3. Exhaustion Reversal (Medium Precision, volume/price extreme)
        
        Higher confidence setups are checked first. If a signal is found, the
        evaluation short-circuits and returns.
        
        Args:
            candle: The latest closed OHLCV candle.
            full_chain: The complete NIFTY option chain from OIFetcher.
            atm: The ATM strikes context including spot price and ATM IV/OI.
            iv_change_pct: The percentage change in ATM Implied Volatility.
            levels: A list of ResistanceLevel objects (structural levels + OI walls) used for target calculation.
            
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
            self.oi_wall_detector.detect(
                spot=atm.spot_price,
                full_chain=full_chain,
                candle=candle
            )
            or
            self.exhaustion_detector.update(
                candle=candle,
                iv_current=atm.ce.iv,
                iv_prev=iv_prev,
                levels=levels
            )
        )

        # 6. Set cooldown if signal fired
        if signal:
            self.last_signal_time = datetime.now()

        # 7. Return the result
        return signal
