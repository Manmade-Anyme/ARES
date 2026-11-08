"""
IV-Gated Dynamic Breakout Detector
==================================
Identifies genuine dynamic range breakouts confirmed by expanding Call IV,
while rejecting vol-crush bull traps and late-stage exhaustion traps.
Operates dynamically across all market sessions without static time boundaries.
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
    Detects dynamic consolidation base breakouts gated by Call IV velocity and VWAP momentum.
    Zero static time-of-day constraints.
    """

    def __init__(self, calibration_policy=None):
        self.calibration_policy = calibration_policy
        self.session_high: float = 0.0
        self.current_date = None
        self.iv_history: deque = deque(maxlen=60)
        self.candle_history: deque = deque(maxlen=60)
        self.fired_today: bool = False

    def reset_session(self, candle_date) -> None:
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

    def observe(
        self,
        candle: OHLCVCandle,
        iv_current: Optional[float] = None,
        strike: Optional[float] = None,
    ) -> None:
        """
        Record candle and IV state across every tick regardless of cooldown or priority.
        Maintains rolling buffers for dynamic base evaluation.
        """
        candle_date, _ = self._parse_time(candle)
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
                self.iv_history.append((candle.timestamp, iv_current, strike))
            elif len(self.iv_history[-1]) > 2 and self.iv_history[-1][2] is None and strike is not None:
                self.iv_history[-1] = (candle.timestamp, iv_current, strike)

    def update(
        self,
        candle: OHLCVCandle,
        avg_volume: float,
        iv_current: Optional[float] = None,
        levels: Optional[List[ResistanceLevel]] = None,
        strike: Optional[float] = None,
    ) -> Optional[AresSignal]:
        if not isinstance(getattr(candle, "timestamp", None), datetime):
            return None

        self.observe(candle, iv_current=iv_current, strike=strike)

        if self.fired_today:
            return None

        lookback = getattr(settings, "iv_breakout_lookback_candles", 25)
        min_warmup = getattr(settings, "iv_breakout_min_warmup_candles", 15)

        # Require minimum session candles to establish dynamic base
        if len(self.candle_history) < max(lookback + 1, min_warmup + 1):
            return None

        # Exclude immediate current candle to determine base resistance
        history_list = list(self.candle_history)
        base_candles = history_list[-(lookback + 1):-1]
        base_high = max(c.high for c in base_candles)
        base_low = min(c.low for c in base_candles)
        base_range = base_high - base_low

        max_base_range = getattr(settings, "iv_breakout_max_base_range_pts", 55.0)
        if base_range > max_base_range:
            return None

        # 1. Dynamic Price Breakout Conditions
        buffer_pts = getattr(settings, "iv_breakout_buffer_pts", 2.0)
        min_zscore = getattr(settings, "iv_breakout_min_zscore", 0.8)
        vol_ratio = getattr(settings, "iv_breakout_volume_ratio", 1.15)

        # Approximate 20-candle standard deviation for Z-score
        recent = history_list[-20:]
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
            candle.close > (base_high + buffer_pts) and
            candle.close > vwap_val and
            candle.close > ema20 and
            vwap_zscore >= min_zscore and
            candle.volume >= (vol_ratio * avg_volume)
        )

        if not is_breakout:
            return None

        # 2. IV Microstructure Velocity Gate
        # Isolate history by strike to prevent cross-strike IV skew being mistaken for a rapid crush
        active_strike = strike
        if active_strike is None and self.iv_history:
            last_entry = self.iv_history[-1]
            if len(last_entry) > 2:
                active_strike = last_entry[2]

        same_strike_samples = []
        for entry in self.iv_history:
            entry_t = entry[0]
            if entry_t == candle.timestamp:
                continue
            entry_v = entry[1]
            entry_s = entry[2] if len(entry) > 2 else None
            if active_strike is not None:
                if entry_s is not None and entry_s != active_strike:
                    continue
            same_strike_samples.append((entry_t, entry_v))

        iv_prev_1m = None
        iv_prev_3m = None
        if same_strike_samples:
            prev_t, prev_v = same_strike_samples[-1]
            if isinstance(candle.timestamp, datetime) and isinstance(prev_t, datetime):
                if (candle.timestamp - prev_t).total_seconds() <= 180:
                    iv_prev_1m = prev_v
            else:
                iv_prev_1m = prev_v

            # Look for sample ~3 minutes prior (between 120s and 360s) or 3 bars back
            for prev_t_cand, prev_v_cand in reversed(same_strike_samples):
                if isinstance(candle.timestamp, datetime) and isinstance(prev_t_cand, datetime):
                    dt_sec = (candle.timestamp - prev_t_cand).total_seconds()
                    if 120 <= dt_sec <= 360:
                        iv_prev_3m = prev_v_cand
                        break
                elif len(same_strike_samples) >= 3 and (prev_t_cand, prev_v_cand) == same_strike_samples[-3]:
                    iv_prev_3m = prev_v_cand
                    break

            if iv_prev_3m is None and len(same_strike_samples) >= 3:
                prev_t3, prev_v3 = same_strike_samples[-3]
                if not (isinstance(candle.timestamp, datetime) and isinstance(prev_t3, datetime)) or (candle.timestamp - prev_t3).total_seconds() <= 360:
                    iv_prev_3m = prev_v3

            if iv_prev_3m is None:
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
            f"Dynamic Base Breakout past {base_high:.2f} ({lookback}-bar consolidation range: {base_range:.1f} pts)",
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


