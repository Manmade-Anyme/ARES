"""Completed five-minute ATR levels for post-T1 trailing.

Supports:
1. Legacy spot ATR aggregation and directional spot stops (`completed_atr_bars`).
2. Pinned option contract premium ATR trailing (`calculate_option_atr_bars`),
   implementing Wilder ATR(5), rolling HHV(10) of (High - 2.5 * ATR5),
   continuous multi-session retention, and upward-only ratchet stops.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from math import isfinite
from typing import Any, Dict, List, Optional, Sequence, Union

from oi_watch_context import parse_candle_timestamp
from options_candles import OptionCandle, load_option_candles

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class OptionATRBar:
    """Calculated ATR and trailing stop levels for one completed 5-minute option bar."""
    start_time: str
    end_time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    atr: Optional[float]
    raw_line: Optional[float]
    formula2_line: Optional[float]
    ready: bool


def calculate_option_atr_bars(
    candles: Union[Sequence[OptionCandle], Sequence[Dict[str, Any]]],
    *,
    atr_period: int = 5,
    hhv_period: int = 10,
    multiplier: float = 2.5,
    reset_on_session: bool = False,
    initial_atr: Optional[float] = None,
    initial_prev_close: Optional[float] = None,
) -> List[OptionATRBar]:
    """Calculate continuous 5-minute Wilder ATR and rolling HHV trailing stops.

    Computes:
        tr_i = max(high_i - low_i, abs(high_i - close_{i-1}), abs(low_i - close_{i-1}))
        atr_i = (atr_{i-1} * (period - 1) + tr_i) / period  (after period-bar SMA seed)
        raw_trail_i = high_i - multiplier * atr_i
        raw_line_i = max(raw_trail_{i - hhv_period + 1} ... raw_trail_i)

    Overnight boundaries alone do not reset the indicator unless reset_on_session is True.
    """
    if not candles:
        return []

    # Ensure candles are OptionCandle objects
    if isinstance(candles[0], OptionCandle):
        sorted_bars: List[OptionCandle] = sorted(candles, key=lambda b: b.start_time)
    else:
        sorted_bars = load_option_candles(candles)

    output: List[OptionATRBar] = []
    ranges: List[float] = []
    trail_window: deque = deque(maxlen=hhv_period)
    high_window: deque = deque(maxlen=hhv_period)

    prev_close: Optional[float] = initial_prev_close
    atr: Optional[float] = initial_atr
    prev_date = sorted_bars[0].start_time.date() if sorted_bars else None

    for bar in sorted_bars:
        curr_date = bar.start_time.date()
        if reset_on_session and prev_date is not None and curr_date != prev_date:
            ranges.clear()
            trail_window.clear()
            high_window.clear()
            prev_close = None
            atr = None
        prev_date = curr_date

        h, l, cl = bar.high, bar.low, bar.close
        high_window.append(h)

        if prev_close is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))

        if atr is None:
            ranges.append(tr)
            if len(ranges) == atr_period:
                atr = sum(ranges) / float(atr_period)
        else:
            atr = (atr * float(atr_period - 1) + tr) / float(atr_period)

        prev_close = cl

        raw_line: Optional[float] = None
        f2_line: Optional[float] = None
        ready = False

        if atr is not None:
            raw_trail = h - multiplier * atr
            trail_window.append(raw_trail)
            if len(trail_window) == hhv_period:
                ready = True
                raw_line = max(trail_window)
                f2_line = max(high_window) - multiplier * atr

        output.append(
            OptionATRBar(
                start_time=bar.timestamp,
                end_time=bar.end_timestamp,
                open=bar.open,
                high=h,
                low=l,
                close=cl,
                volume=bar.volume,
                atr=round(atr, 6) if atr is not None else None,
                raw_line=round(raw_line, 6) if raw_line is not None else None,
                formula2_line=round(f2_line, 6) if f2_line is not None else None,
                ready=ready,
            )
        )

    return output


def ratchet_option_stop(
    current_stop: Optional[float],
    new_raw_line: Optional[float],
    original_option_sl: float,
    *,
    protection_mode: str = "NONE",
) -> tuple[float, str, bool]:
    """Apply upward-only ratchet rules for option premium protection.

    Rules:
    - Pre-activation / activation check: raw_line must be strictly above original_option_sl.
    - Once active: locked_stop = max(original_option_sl, previous_locked_stop, newly_confirmed_raw_line).
    - A falling raw line never lowers the locked stop.
    - If protection_mode == 'ORIGINAL_OPTION_SL_FLOOR' (fallback after T2 when ATR hasn't qualified),
      locked_stop is original_option_sl.

    Returns:
        (locked_stop, new_protection_mode, did_ratchet_increase)
    """
    if protection_mode == "ORIGINAL_OPTION_SL_FLOOR":
        base_floor = float(original_option_sl)
        if new_raw_line is not None and new_raw_line > base_floor:
            # Upgrade from floor fallback to active ATR
            new_stop = max(base_floor, float(current_stop or 0.0), new_raw_line)
            increased = current_stop is None or new_stop > current_stop
            return (new_stop, "ATR", increased)
        locked = max(base_floor, float(current_stop or 0.0))
        return (locked, "ORIGINAL_OPTION_SL_FLOOR", current_stop is None or locked > current_stop)

    if protection_mode == "ATR":
        base = max(float(original_option_sl), float(current_stop or 0.0))
        if new_raw_line is not None and new_raw_line > base:
            return (new_raw_line, "ATR", True)
        return (base, "ATR", False)

    # Currently protection_mode is 'NONE'
    if new_raw_line is not None and new_raw_line > float(original_option_sl):
        # Initial ATR activation: strictly above original option SL
        return (new_raw_line, "ATR", True)

    return (float(current_stop or 0.0), "NONE", False)


def completed_atr_bars(candles):
    """Aggregate explicit completed minute bars and calculate Wilder ATR(5).

    Legacy helper preserved for spot-index trailing stop callers.
    Timestamps identify source bar starts; output timestamps identify five-minute
    bar ends in IST. Gaps and session changes reset ATR and the ten-bar extremes.
    Stop levels are raw candidates; callers enforce entry and ratchet bounds.
    """
    minutes = {}
    for candle in candles or ():
        if not isinstance(candle, dict):
            continue
        try:
            at = parse_candle_timestamp(candle.get("timestamp"))
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        if at.second or at.microsecond or not time(9, 15) <= at.time() < time(15, 30):
            continue
        # A malformed or forming revision must invalidate a previously good row.
        minutes[at] = None
        if candle.get("complete") is not True:
            continue
        try:
            values = [float(candle[key]) for key in ("open", "high", "low", "close")]
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if any(isinstance(candle[key], bool) for key in ("open", "high", "low", "close")):
            continue
        if not all(isfinite(value) and value > 0 for value in values):
            continue
        opening, high, low, close = values
        if high < max(opening, low, close) or low > min(opening, close):
            continue
        minutes[at] = (high, low, close)

    starts = sorted({at.replace(minute=at.minute // 5 * 5) for at in minutes})
    output, ranges = [], []
    window = deque(maxlen=10)
    previous_end = previous_close = atr = None
    for start in starts:
        source = [minutes.get(start + timedelta(minutes=i)) for i in range(5)]
        if any(row is None for row in source):
            continue
        if previous_end != start:
            ranges.clear()
            window.clear()
            previous_close = atr = None
        high = max(row[0] for row in source)
        low = min(row[1] for row in source)
        close = source[-1][2]
        tr = high - low if previous_close is None else max(
            high - low, abs(high - previous_close), abs(low - previous_close)
        )
        if atr is None:
            ranges.append(tr)
            if len(ranges) == 5:
                atr = sum(ranges) / 5
        else:
            atr = (atr * 4 + tr) / 5
        window.append((high, low))
        previous_end = start + timedelta(minutes=5)
        previous_close = close
        ready = len(window) == 10
        output.append({
            "timestamp": previous_end.isoformat(), "close": close, "atr": atr,
            "bullish_sl": max(row[0] for row in window) - 2.5 * atr if ready else None,
            "bearish_sl": min(row[1] for row in window) + 2.5 * atr if ready else None,
        })
    return output
