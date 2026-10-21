"""Completed five-minute spot ATR levels for post-T1 trailing."""
from collections import deque
from datetime import time, timedelta
from math import isfinite

from oi_watch_context import parse_candle_timestamp


def completed_atr_bars(candles):
    """Aggregate explicit completed minute bars and calculate Wilder ATR(5).

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
