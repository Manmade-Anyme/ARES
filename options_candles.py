"""Pinned option contract candle ingestion and validation.

Normalizes provider candles for pinned contracts to completed 5-minute bars
with explicit start and end timestamps, robust boundary validation, duplicate
resolution, and continuous multi-session retention for indicator warmup.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from math import isfinite
from typing import Any, Dict, List, Optional, Sequence, Union

from oi_watch_context import parse_candle_timestamp

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class OptionCandle:
    """An immutable, verified 5-minute option candle."""
    start_time: datetime
    end_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    security_id: Optional[str] = None
    complete: bool = True

    @property
    def start(self) -> datetime:
        return self.start_time

    @property
    def end(self) -> datetime:
        return self.end_time

    @property
    def timestamp(self) -> str:
        """ISO 8601 string of the bar start in IST."""
        return self.start_time.isoformat()

    @property
    def end_timestamp(self) -> str:
        """ISO 8601 string of the bar end in IST."""
        return self.end_time.isoformat()


Candle5m = OptionCandle


def _is_valid_price(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        val = float(value)
        return isfinite(val) and val > 0.0
    except (TypeError, ValueError, OverflowError):
        return False


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def parse_option_candle(
    raw: Union[Dict[str, Any], OptionCandle],
    *,
    observed_at: Optional[datetime] = None,
    expected_security_id: Optional[str] = None,
    timeframe_minutes: int = 5,
) -> Optional[OptionCandle]:
    """Parse and strictly validate a single option candle dictionary.

    Rejects malformed OHLC, forming/uncompleted candles, and mismatched contracts.
    """
    if isinstance(raw, OptionCandle):
        raw = {"start_time": raw.start_time, "end_time": raw.end_time,
               "open": raw.open, "high": raw.high, "low": raw.low,
               "close": raw.close, "volume": raw.volume,
               "security_id": raw.security_id, "complete": raw.complete}
    if not isinstance(raw, dict):
        return None

    # Contract verification if security_id is attached to candle
    cand_sec_id = raw.get("security_id")
    if cand_sec_id is not None and expected_security_id is not None:
        if str(cand_sec_id).strip() != str(expected_security_id).strip():
            return None

    # Timestamp extraction and normalization
    raw_ts = raw.get("timestamp") or raw.get("start_Time") or raw.get("start_time") or raw.get("start")
    if raw_ts is None:
        return None
    try:
        start_dt = parse_candle_timestamp(raw_ts)
    except (ValueError, TypeError, OverflowError, OSError):
        return None

    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=IST)
    else:
        start_dt = start_dt.astimezone(IST)

    # Derive explicit candle end
    end_dt = start_dt + timedelta(minutes=timeframe_minutes)

    if timeframe_minutes not in (1, 5):
        return None
    if (start_dt.second or start_dt.microsecond
            or start_dt.minute % timeframe_minutes
            or not time(9, 15) <= start_dt.time() < time(15, 30)
            or end_dt.time() > time(15, 30)):
        return None
    explicit_end = raw.get("end_time") or raw.get("end")
    if explicit_end is not None:
        try:
            supplied_end = parse_candle_timestamp(explicit_end)
            supplied_end = supplied_end.replace(tzinfo=IST) if supplied_end.tzinfo is None else supplied_end.astimezone(IST)
            if supplied_end != end_dt:
                return None
        except (ValueError, TypeError, OverflowError, OSError):
            return None

    # Completion validation against observation time
    if observed_at is not None:
        obs_ist = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=IST)
        obs_ist = obs_ist.astimezone(IST)
        if obs_ist < end_dt:
            # Forming candle: not yet completed
            return None

    # If the input explicitly flagged complete=False, reject
    if raw.get("complete") is False:
        return None

    # Price validations
    for key in ("open", "high", "low", "close"):
        if not _is_valid_price(raw.get(key)):
            return None

    op = float(raw["open"])
    hi = float(raw["high"])
    lo = float(raw["low"])
    cl = float(raw["close"])
    vol = max(0.0, _to_float(raw.get("volume", 0.0)))

    # Geometric consistency: High must be >= max(O, L, C); Low must be <= min(O, C)
    if hi < max(op, lo, cl) or lo > min(op, cl):
        return None

    return OptionCandle(
        start_time=start_dt,
        end_time=end_dt,
        open=op,
        high=hi,
        low=lo,
        close=cl,
        volume=vol,
        security_id=str(cand_sec_id) if cand_sec_id is not None else expected_security_id,
        complete=True,
    )


def load_option_candles(
    candles: Sequence[Union[Dict[str, Any], OptionCandle]],
    *,
    observed_at: Optional[datetime] = None,
    expected_security_id: Optional[str] = None,
    timeframe_minutes: int = 5,
) -> List[OptionCandle]:
    """Ingest, validate, deduplicate, and sort a sequence of option candles.

    Supports native 5-minute candles and handles revisions.
    Overnight session boundaries are preserved without artificial filtering.
    """
    if not candles:
        return []

    parsed_by_start: Dict[datetime, OptionCandle] = {}

    for row in candles:
        parsed = parse_option_candle(
            row,
            observed_at=observed_at,
            expected_security_id=expected_security_id,
            timeframe_minutes=timeframe_minutes,
        )
        if parsed is None:
            continue

        # In case of duplicates, keep the last valid revision
        parsed_by_start[parsed.start_time] = parsed

    # Return chronologically sorted
    return [parsed_by_start[k] for k in sorted(parsed_by_start.keys())]


def aggregate_minute_candles_to_5m(
    minute_candles: Sequence[Dict[str, Any]],
    *,
    observed_at: Optional[datetime] = None,
    expected_security_id: Optional[str] = None,
) -> List[OptionCandle]:
    """Aggregate contiguous 1-minute option candles into completed 5-minute candles.

    Requires exactly 5 contiguous completed 1-minute bars for each 5-minute bucket.
    """
    if not minute_candles:
        return []

    valid_minutes: Dict[datetime, Dict[str, float]] = {}
    for row in minute_candles:
        parsed = parse_option_candle(row, observed_at=observed_at,
                                     expected_security_id=expected_security_id,
                                     timeframe_minutes=1)
        if parsed is not None:
            valid_minutes[parsed.start_time] = {
                "open": parsed.open, "high": parsed.high, "low": parsed.low,
                "close": parsed.close, "volume": parsed.volume,
            }

    # Group into 5-minute buckets
    buckets: Dict[datetime, List[Dict[str, float]]] = {}
    for dt, vals in sorted(valid_minutes.items()):
        bucket_start = dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)
        expected_offset = dt - bucket_start
        # Only accept standard 5m alignment
        if expected_offset < timedelta(minutes=5):
            buckets.setdefault(bucket_start, []).append(vals)

    aggregated: List[OptionCandle] = []
    for bucket_start, bars in sorted(buckets.items()):
        if len(bars) != 5:
            # Incomplete 5m bucket (gap or forming)
            continue
        bucket_end = bucket_start + timedelta(minutes=5)
        if observed_at is not None:
            obs = observed_at.astimezone(IST) if observed_at.tzinfo else observed_at.replace(tzinfo=IST)
            if obs < bucket_end:
                continue

        op = bars[0]["open"]
        hi = max(b["high"] for b in bars)
        lo = min(b["low"] for b in bars)
        cl = bars[-1]["close"]
        vol = sum(b["volume"] for b in bars)

        aggregated.append(
            OptionCandle(
                start_time=bucket_start,
                end_time=bucket_end,
                open=op,
                high=hi,
                low=lo,
                close=cl,
                volume=vol,
                security_id=expected_security_id,
                complete=True,
            )
        )

    return aggregated


aggregate_to_5min = aggregate_minute_candles_to_5m


def contiguous_option_candles(candles: Sequence[OptionCandle]) -> List[OptionCandle]:
    """Return the continuous prefix; a missing in-session bar blocks advancement.

    A full exchange close to next observed session open carries indicator state.
    Missing tail/head bars across sessions also require backfill. Holiday dates do
    not reset the indicator or require synthetic zero-volume candles.
    """
    contiguous: List[OptionCandle] = []
    for bar in sorted(candles, key=lambda value: value.start_time):
        if contiguous:
            previous = contiguous[-1]
            overnight = (bar.start_time.date() > previous.start_time.date()
                         and previous.end_time.time() == time(15, 30)
                         and bar.start_time.time() == time(9, 15))
            if bar.start_time != previous.end_time and not overnight:
                break
        contiguous.append(bar)
    return contiguous


def expected_completed_option_bar_end(observed_at: datetime) -> Optional[datetime]:
    """Latest expected session bar close; outside session there is no outage clock."""
    now = observed_at.replace(tzinfo=IST) if observed_at.tzinfo is None else observed_at.astimezone(IST)
    if now.weekday() >= 5 or not time(9, 20) <= now.time() <= time(15, 30, 59):
        return None
    return now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
