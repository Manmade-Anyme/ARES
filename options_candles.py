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
    raw: Dict[str, Any],
    *,
    observed_at: Optional[datetime] = None,
    expected_security_id: Optional[str] = None,
    timeframe_minutes: int = 5,
) -> Optional[OptionCandle]:
    """Parse and strictly validate a single option candle dictionary.

    Rejects malformed OHLC, forming/uncompleted candles, and mismatched contracts.
    """
    if not isinstance(raw, dict):
        return None

    # Contract verification if security_id is attached to candle
    cand_sec_id = raw.get("security_id")
    if cand_sec_id is not None and expected_security_id is not None:
        if str(cand_sec_id).strip() != str(expected_security_id).strip():
            return None

    # Timestamp extraction and normalization
    raw_ts = raw.get("timestamp") or raw.get("start_Time") or raw.get("start_time")
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
    candles: Sequence[Dict[str, Any]],
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
        if not isinstance(row, dict):
            continue
        if row.get("complete") is False:
            continue
        cand_sec_id = row.get("security_id")
        if cand_sec_id is not None and expected_security_id is not None:
            if str(cand_sec_id).strip() != str(expected_security_id).strip():
                continue
        raw_ts = row.get("timestamp") or row.get("start_Time") or row.get("start_time")
        if raw_ts is None:
            continue
        try:
            dt = parse_candle_timestamp(raw_ts)
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        else:
            dt = dt.astimezone(IST)

        if not all(_is_valid_price(row.get(k)) for k in ("open", "high", "low", "close")):
            continue
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        if h < max(o, l, c) or l > min(o, c):
            continue
        vol = max(0.0, _to_float(row.get("volume", 0.0)))
        valid_minutes[dt] = {"open": o, "high": h, "low": l, "close": c, "volume": vol}

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
