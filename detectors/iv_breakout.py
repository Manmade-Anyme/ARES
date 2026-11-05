"""
IV-Gated Breakout Expansion Detector
====================================
Identifies true afternoon range breakouts confirmed by expanding Call IV,
while rejecting vol-crush bull traps and late-stage exhaustion traps.
"""

from collections import deque
from datetime import datetime
import math
from typing import Optional, List, Dict, Any, Tuple

from models import (
    OHLCVCandle,
    ResistanceLevel,
    AresSignal,
    SetupType,
    Direction,
)
from config import settings
from options_math import evaluate_iv_velocity


class IVBreakoutDetector:
    """
    Detects Afternoon Range Breakouts (12:30 - 14:15) gated by Call IV velocity.
    """

    def __init__(self, calibration_policy=None):
        self.calibration_policy = calibration_policy
        self.anchor_high: float = 0.0
        self.session_high: float = 0.0
        self.current_date = None
        self.iv_history: deque = deque(maxlen=60)
        self.candle_history: deque = deque(maxlen=60)
        self.fired_today: bool = False

    def reset_session(self, candle_date) -> None:
        self.anchor_high = 0.0
        self.session_high = 0.0
        self.current_date = candle_date
        self.iv_history.clear()
        self.candle_history.clear()
        self.fired_today = False

    def _parse_time(self, candle: OHLCVCandle) -> Tuple[Optional[Any], str]:
        """Defensively extract session date and HH:MM time string."""
        ts = getattr(candle, "timestamp", None)
        if isinstance(ts, datetime):
            return ts.date(), ts.strftime("%H:%M")
        if isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return dt.date(), dt.strftime("%H:%M")
            except Exception:
                return None, ts[11:16] if len(ts) >= 16 else "12:00"
        if hasattr(ts, "strftime"):
            try:
                val = ts.strftime("%H:%M")
                if isinstance(val, str):
                    c_date = getattr(ts, "date", lambda: None)()
                    return c_date, val
            except Exception:
                pass
        return None, "12:00"

    def observe(self, candle: OHLCVCandle, iv_current: Optional[float] = None) -> None:
        """
        Record candle and IV state across every tick regardless of cooldown or priority.
        Prevents blind spots in morning anchor high tracking.
        """
        candle_date, time_str = self._parse_time(candle)
        if candle_date is not None and self.current_date != candle_date:
            self.reset_session(candle_date)

        try:
            self.session_high = max(self.session_high, float(candle.high))
        except (TypeError, ValueError):
            pass

        # Avoid duplicating candle history if observe and update are called on the same tick
        if not self.candle_history or self.candle_history[-1] is not candle:
            self.candle_history.append(candle)

        if iv_current is not None and iv_current > 0:
            if not self.iv_history or self.iv_history[-1][0] != candle.timestamp:
                self.iv_history.append((candle.timestamp, iv_current))

        anchor_end = getattr(settings, "iv_breakout_anchor_end", "12:30")
        if time_str <= anchor_end:
            try:
                self.anchor_high = max(self.anchor_high, float(candle.high))
            except (TypeError, ValueError):
                pass

    def update(
        self,
        candle: OHLCVCandle,
        avg_volume: float,
        iv_current: Optional[float] = None,
        levels: Optional[List[ResistanceLevel]] = None,
    ) -> Optional[AresSignal]:
        if not isinstance(getattr(candle, "timestamp", None), datetime):
            return None

        self.observe(candle, iv_current=iv_current)

        _, time_str = self._parse_time(candle)
        anchor_end = getattr(settings, "iv_breakout_anchor_end", "12:30")
        if time_str <= anchor_end:
            return None

        # Entry window: 12:30 to 14:15
        cutoff_time = getattr(settings, "iv_breakout_cutoff_time", "14:15")
        if not (anchor_end < time_str <= cutoff_time):
            return None

        if self.anchor_high <= 0 or self.fired_today:
            return None

        # 1. Price Breakout Conditions
        buffer_pts = getattr(settings, "iv_breakout_buffer_pts", 2.0)
        min_zscore = getattr(settings, "iv_breakout_min_zscore", 0.8)

        # Approximate 20-candle standard deviation for Z-score
        recent = list(self.candle_history)[-20:]
        if len(recent) >= 5:
            mean_c = sum(c.close for c in recent) / len(recent)
            variance = sum((c.close - mean_c) ** 2 for c in recent) / len(recent)
            rolling_std = max(1.0, math.sqrt(variance))
        else:
            rolling_std = 15.0

        if getattr(candle, "vwap", 0.0) and candle.vwap > 0:
            vwap_val = candle.vwap
        elif len(self.candle_history) >= 5:
            tot_vol = sum(c.volume for c in self.candle_history)
            vwap_val = sum(c.close * c.volume for c in self.candle_history) / tot_vol if tot_vol > 0 else candle.close
        else:
            vwap_val = candle.close

        vwap_zscore = (candle.close - vwap_val) / rolling_std

        # 20 EMA approximation from recent closes
        closes = [c.close for c in recent]
        ema20 = sum(closes) / len(closes) if closes else candle.close

        is_breakout = (
            candle.close > (self.anchor_high + buffer_pts) and
            candle.close > vwap_val and
            candle.close > ema20 and
            vwap_zscore >= min_zscore and
            candle.volume >= 0.95 * avg_volume
        )

        if not is_breakout:
            return None

        # 2. IV Microstructure Velocity Gate
        iv_prev_1m = None
        iv_prev_3m = None
        if len(self.iv_history) >= 2:
            prev_t, prev_v = self.iv_history[-2]
            if isinstance(candle.timestamp, datetime) and isinstance(prev_t, datetime):
                if (candle.timestamp - prev_t).total_seconds() <= 180:
                    iv_prev_1m = prev_v
            else:
                iv_prev_1m = prev_v

        if len(self.iv_history) >= 4:
            prev_t3, prev_v3 = self.iv_history[-4]
            if isinstance(candle.timestamp, datetime) and isinstance(prev_t3, datetime):
                if (candle.timestamp - prev_t3).total_seconds() <= 360:
                    iv_prev_3m = prev_v3
            else:
                iv_prev_3m = prev_v3
        else:
            iv_prev_3m = iv_prev_1m

        iv_ok, iv_reason = evaluate_iv_velocity(
            iv_current=iv_current,
            iv_prev_1m=iv_prev_1m,
            iv_prev_3m=iv_prev_3m,
            vwap_zscore=vwap_zscore,
        )

        if not iv_ok:
            return None

        # 3. Construct Signal
        self.fired_today = True
        strike_to_trade = int(round(candle.close / settings.strike_interval) * settings.strike_interval)
        entry_zone = (
            candle.close - settings.entry_zone_offset_pts,
            candle.close + settings.entry_zone_offset_pts
        )

        reasons = [
            f"Afternoon Range Breakout past {self.anchor_high:.2f}",
            f"VWAP Z-Score {vwap_zscore:.2f} confirms directional momentum",
            f"IV Velocity confirmed ({iv_reason})"
        ]

        signal = AresSignal(
            setup_type=SetupType.IV_GATED_BREAKOUT,
            direction=Direction.BULLISH,
            trigger_price=candle.close,
            entry_zone=entry_zone,
            stop_loss=0.0,   # Set centrally by apply_per_type_levels
            target_1=0.0,    # Set centrally by apply_per_type_levels
            target_2=0.0,    # Set centrally by apply_per_type_levels
            confidence="HIGH",
            reasons=reasons,
            timestamp=candle.timestamp,
            strike_to_trade=strike_to_trade,
            option_type="CE"
        )

        return self.calibration_policy.apply_to_signal(signal) if self.calibration_policy else signal

