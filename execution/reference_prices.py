"""Select timestamped, provenance-bearing prices for reference accounting.

Minute timestamps denote bar starts. A completed close is usable only when the
provider sampled that bar after its end; aging a previously forming bar does not
make it completed evidence.
"""
from datetime import datetime, time, timedelta, timezone
from math import isfinite

from oi_watch_context import parse_candle_timestamp


def _timestamp(value):
    try:
        return parse_candle_timestamp(value)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _price(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if isfinite(value) and value > 0 else None


def normalize_option_minutes(data, session_date, sampled_at):
    """Normalize a single pinned-request response without aging forming rows."""
    sampled = _timestamp(sampled_at)
    if sampled is None or not isinstance(data, dict):
        return []
    time_key = "start_Time" if "start_Time" in data else "timestamp"
    keys = (time_key, "open", "high", "low", "close", "volume")
    if any(not isinstance(data.get(key), (list, tuple)) for key in keys):
        return []
    if any(len(data[key]) != len(data[time_key]) for key in keys):
        return []
    revisions = {}
    for index, timestamp in enumerate(data[time_key]):
        at = _timestamp(timestamp)
        if (at is None or at.date() != session_date or at.second or at.microsecond
                or not time(9, 15) <= at.time() < time(15, 30) or at > sampled):
            continue
        # Later invalid revisions invalidate, rather than resurrect, older bars.
        row = {"timestamp": at.isoformat(), "sampled_at": sampled.isoformat(), "complete": False}
        revisions[at] = row
        prices = [_price(data[key][index]) for key in ("open", "high", "low", "close")]
        volume = data["volume"][index]
        try:
            volume_valid = not isinstance(volume, bool) and isfinite(float(volume)) and float(volume) >= 0
        except (TypeError, ValueError, OverflowError):
            volume_valid = False
        if any(value is None for value in prices) or not volume_valid:
            continue
        opening, high, low, close = prices
        if high < max(opening, low, close) or low > min(opening, close):
            continue
        row.update({"open": opening, "high": high, "low": low, "close": close,
                    "volume": float(volume), "complete": at + timedelta(minutes=1) <= sampled})
    return [revisions[at] for at in sorted(revisions)]


def _minute_close(candles, minute, observed_at):
    """Return a close only if the latest revision is valid and complete."""
    selected = None
    for row in candles or ():
        if not isinstance(row, dict) or _timestamp(row.get("timestamp")) != minute:
            continue
        selected = None
        sampled = _timestamp(row.get("sampled_at"))
        if (row.get("complete") is not True or sampled is None
                or sampled < minute + timedelta(minutes=1) or sampled > observed_at):
            continue
        values = [_price(row.get(key)) for key in ("open", "high", "low", "close")]
        if any(value is None for value in values):
            continue
        opening, high, low, close = values
        if high < max(opening, low, close) or low > min(opening, close):
            continue
        selected = {"price": close, "observed_at": (minute + timedelta(minutes=1)).isoformat(),
                    "source": "completed_1m_close", "approximate": True}
    return selected


def _nearest_observation(observations, event, observed_at):
    candidates = []
    for row in observations or ():
        if not isinstance(row, dict):
            continue
        at, price = _timestamp(row.get("observed_at")), _price(row.get("price"))
        source = row.get("source")
        if at is None or price is None or not isinstance(source, str) or not source.strip():
            continue
        if at > observed_at or abs((at - event).total_seconds()) > 5:
            continue
        candidates.append((abs((at - event).total_seconds()), at,
                           {"price": price, "observed_at": at.isoformat(),
                            "source": source, "approximate": False}))
    return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def option_target_evidence(event_at, *, observations=(), option_minutes=(), source_minute=None, observed_at=None):
    """Select a nearby quote, or the matching complete option-minute close.

    Set event_at to None for wick-only detection, supplying the source minute's
    start. Without a known touch time a nearby quote is never associated.
    """
    now = _timestamp(observed_at or datetime.now(timezone.utc))
    event = _timestamp(event_at) if event_at is not None else None
    minute = _timestamp(source_minute) if source_minute is not None else None
    if now is None or (event_at is not None and event is None):
        return None
    if event is not None:
        if event > now:
            return None
        if minute is not None and not minute <= event < minute + timedelta(minutes=1):
            return None
        quote = _nearest_observation(observations, event, now)
        if quote:
            return quote
        if minute is None:
            minute = event.replace(second=0, microsecond=0)
    if minute is None or minute.second or minute.microsecond:
        return None
    if not time(9, 15) <= minute.time() < time(15, 30):
        return None
    return _minute_close(option_minutes, minute, now)


def spot_exit_evidence(exit_at, *, spot_minutes=(), observations=(), observed_at=None):
    """Find the spot minute ending at an ATR close, else a nearby quote."""
    event = _timestamp(exit_at)
    now = _timestamp(observed_at or datetime.now(timezone.utc))
    if event is None or now is None or event > now or event.second or event.microsecond:
        return None
    minute = event - timedelta(minutes=1)
    if not time(9, 15) <= minute.time() < time(15, 30):
        return None
    return (_minute_close(spot_minutes, minute, now)
            or _nearest_observation(observations, event, now))
