from collections import deque
from datetime import datetime, timedelta
from statistics import mean
from typing import Optional, List, Dict, Any

from models import OHLCVCandle, ATMStrikes, AresSignal, ResistanceLevel, Direction, SetupType
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
        # IV percentile lookbacks for the anti-IV-crush filter. Tracked per
        # option side (bullish entries buy CEs, bearish entries buy PEs) so the
        # filter can act symmetrically (TASK-172, audit item 10).
        self.iv_lookback: deque = deque(maxlen=settings.iv_crush_lookback_size)
        self.pe_iv_lookback: deque = deque(maxlen=settings.iv_crush_lookback_size)

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
        self.iv_lookback.append(atm.ce.iv)
        self.pe_iv_lookback.append(atm.pe.iv)

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
            self.exhaustion_detector.update(
                candle=candle,
                iv_current=atm.ce.iv,
                iv_prev=iv_prev,
                levels=levels
            )
        )

        # 6. Apply protective filters
        if signal:
            # Filter A: Speed Filter (suppress MEDIUM confidence in flat market)
            # Window and threshold are profile-tunable (TASK-172, audit item 12).
            window = settings.speed_filter_window_candles
            is_market_too_slow = False
            rolling_range = 0.0
            if len(self.candle_buffer) >= window:
                recent = list(self.candle_buffer)[-window:]
                highs = [c.high for c in recent]
                lows = [c.low for c in recent]
                rolling_range = max(highs) - min(lows)
                is_market_too_slow = rolling_range < settings.speed_filter_min_range_pts

            if signal.confidence == "MEDIUM" and is_market_too_slow:
                print(f"[-] AresEngine: Suppressing {signal.setup_type.value} ({signal.direction.value}) signal. Reason: Sluggish market ({window}-min range: {rolling_range:.2f} pts).")
                signal = None

        if signal:
            # Filter C: Risk:Reward Gate (reject setups whose risk to SL exceeds reward to T1)
            risk = abs(signal.trigger_price - signal.stop_loss)
            reward = abs(signal.target_1 - signal.trigger_price)
            # Degenerate SL placement (zero/negative risk) is always rejected
            if risk <= 0 or (reward / risk) < settings.min_rr_ratio:
                rr = (reward / risk) if risk > 0 else 0.0
                print(f"[-] AresEngine: Suppressing {signal.setup_type.value} ({signal.direction.value}) signal. Reason: R:R {rr:.2f} below minimum {settings.min_rr_ratio:.2f} (risk {risk:.1f} pts vs reward {reward:.1f} pts).")
                signal = None

        if signal:
            # Filter D: Exhaustion observation mode — alert + log, but never trade
            if signal.setup_type == SetupType.EXHAUSTION_REVERSAL and settings.exhaustion_alert_only:
                signal.alert_only = True
                signal.reasons.append("Observation only — exhaustion entries gated by config (exhaustion_alert_only)")

        if signal and signal.confidence == "MEDIUM" and not signal.alert_only:
            # Filter B: Anti-IV Crush Filter (TASK-172, audit item 10).
            # Suppress MEDIUM-confidence entries whose option side shows
            # top-percentile IV — HIGH confidence setups are exempt (the old
            # filter killed every bullish signal regardless of quality), and
            # the check is symmetric: bullish entries buy CEs so they check CE
            # IV, bearish entries buy PEs so they check PE IV. Observation-only
            # signals pass through — they are never traded, and suppressing
            # them would just lose exhaustion observation data.
            if signal.direction == Direction.BULLISH:
                lookback, current_iv, side = self.iv_lookback, atm.ce.iv, "CE"
            else:
                lookback, current_iv, side = self.pe_iv_lookback, atm.pe.iv, "PE"

            if len(lookback) >= 10:
                lower_iv_count = sum(1 for x in lookback if x < current_iv)
                percentile = (lower_iv_count / len(lookback)) * 100.0
                if percentile >= settings.iv_crush_percentile:
                    print(f"[-] AresEngine: Suppressing {signal.setup_type.value} ({signal.direction.value}) signal. Reason: {side} IV {current_iv:.2f}% in top {100.0 - settings.iv_crush_percentile:.0f}% of lookback poses high risk of IV crush.")
                    signal = None

        # 7. Set cooldown if signal fired.
        # Observation-only signals don't consume the cooldown — they must never
        # block a tradeable setup from another detector.
        if signal and not signal.alert_only:
            self.last_signal_time = datetime.now()

        # 8. Return the result
        return signal

    def clear_cooldown(self) -> None:
        """
        Reset the signal cooldown. Called after a trade stops out so a fresh
        setup can be taken immediately instead of waiting out the timer.
        """
        self.last_signal_time = None
