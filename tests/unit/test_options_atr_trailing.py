"""Unit tests for pinned option candle ingestion and pure option ATR calculations."""
from datetime import datetime, timedelta, timezone
import pytest

from options_candles import (
    OptionCandle,
    aggregate_minute_candles_to_5m,
    load_option_candles,
    parse_option_candle,
)
from atr_trailing import (
    OptionATRBar,
    calculate_option_atr_bars,
    ratchet_option_stop,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _sample_raw_candle(
    start_str="2026-10-23T09:15:00+05:30",
    op=100.0,
    hi=110.0,
    lo=95.0,
    cl=105.0,
    vol=1000.0,
    sec_id="51333",
    complete=True,
):
    return {
        "timestamp": start_str,
        "open": op,
        "high": hi,
        "low": lo,
        "close": cl,
        "volume": vol,
        "security_id": sec_id,
        "complete": complete,
    }


def test_parse_option_candle_valid():
    raw = _sample_raw_candle()
    candle = parse_option_candle(raw, expected_security_id="51333")
    assert candle is not None
    assert candle.open == 100.0
    assert candle.high == 110.0
    assert candle.low == 95.0
    assert candle.close == 105.0
    assert candle.volume == 1000.0
    assert candle.security_id == "51333"
    assert candle.start_time.isoformat() == "2026-10-23T09:15:00+05:30"
    assert candle.end_time.isoformat() == "2026-10-23T09:20:00+05:30"


def test_parse_option_candle_forming_rejected():
    raw = _sample_raw_candle(start_str="2026-10-23T09:15:00+05:30")
    # Observed before 5-minute end time
    obs = datetime(2026, 10, 23, 9, 18, tzinfo=IST)
    candle = parse_option_candle(raw, observed_at=obs)
    assert candle is None

    # Observed at or after end time -> accepted
    obs_valid = datetime(2026, 10, 23, 9, 20, tzinfo=IST)
    candle_valid = parse_option_candle(raw, observed_at=obs_valid)
    assert candle_valid is not None


def test_parse_option_candle_explicit_incomplete_rejected():
    raw = _sample_raw_candle(complete=False)
    assert parse_option_candle(raw) is None


def test_parse_option_candle_wrong_contract_rejected():
    raw = _sample_raw_candle(sec_id="51331")
    assert parse_option_candle(raw, expected_security_id="51333") is None


def test_parse_option_candle_invalid_prices_rejected():
    # Negative price
    assert parse_option_candle(_sample_raw_candle(op=-10.0)) is None
    # Zero price
    assert parse_option_candle(_sample_raw_candle(cl=0.0)) is None
    # Non-finite price
    assert parse_option_candle(_sample_raw_candle(hi=float("nan"))) is None
    # Boolean price
    assert parse_option_candle(_sample_raw_candle(lo=True)) is None
    # High lower than close
    assert parse_option_candle(_sample_raw_candle(hi=100.0, cl=105.0)) is None
    # Low higher than open
    assert parse_option_candle(_sample_raw_candle(lo=102.0, op=100.0)) is None
    # Malformed non-dict
    assert parse_option_candle("invalid") is None  # type: ignore


def test_load_option_candles_dedup_and_sort():
    # Two revisions of 09:15 bar, second revision has higher volume
    c1 = _sample_raw_candle("2026-10-23T09:15:00+05:30", vol=100.0)
    c2 = _sample_raw_candle("2026-10-23T09:15:00+05:30", vol=500.0)
    c3 = _sample_raw_candle("2026-10-23T09:20:00+05:30", vol=200.0)
    # Provided out of order
    candles = load_option_candles([c3, c1, c2], expected_security_id="51333")
    assert len(candles) == 2
    assert candles[0].start_time.isoformat() == "2026-10-23T09:15:00+05:30"
    assert candles[0].volume == 500.0
    assert candles[1].start_time.isoformat() == "2026-10-23T09:20:00+05:30"


def test_load_option_candles_continuous_multi_session():
    # Day 1 bar and Day 2 bar are both retained
    day1 = _sample_raw_candle("2026-10-22T15:25:00+05:30")
    day2 = _sample_raw_candle("2026-10-23T09:15:00+05:30")
    candles = load_option_candles([day1, day2])
    assert len(candles) == 2
    assert candles[0].start_time.date() != candles[1].start_time.date()


def test_aggregate_minute_candles_to_5m():
    minutes = []
    base = datetime(2026, 10, 23, 9, 15, tzinfo=IST)
    for i in range(5):
        ts = (base + timedelta(minutes=i)).isoformat()
        minutes.append({
            "timestamp": ts,
            "open": 100.0 + i,
            "high": 105.0 + i,
            "low": 98.0 + i,
            "close": 101.0 + i,
            "volume": 100.0,
            "complete": True,
            "security_id": "51333",
        })
    # Exactly 5 bars -> 1 completed 5m bar
    five_m = aggregate_minute_candles_to_5m(minutes, expected_security_id="51333")
    assert len(five_m) == 1
    b = five_m[0]
    assert b.open == 100.0
    assert b.high == 109.0
    assert b.low == 98.0
    assert b.close == 105.0
    assert b.volume == 500.0
    assert b.start_time.isoformat() == "2026-10-23T09:15:00+05:30"
    assert b.end_time.isoformat() == "2026-10-23T09:20:00+05:30"

    # Incomplete bucket (e.g. only 4 bars) -> rejected
    assert aggregate_minute_candles_to_5m(minutes[:4]) == []


def test_calculate_option_atr_bars_wilder_and_hhv():
    bars = []
    base = datetime(2026, 10, 23, 9, 15, tzinfo=IST)
    # Generate 15 consecutive 5m bars
    for i in range(15):
        st = base + timedelta(minutes=i * 5)
        et = st + timedelta(minutes=5)
        bars.append(
            OptionCandle(
                start_time=st,
                end_time=et,
                open=100.0 + i,
                high=110.0 + i,
                low=90.0 + i,
                close=105.0 + i,
                volume=1000.0,
            )
        )

    res = calculate_option_atr_bars(bars, atr_period=5, hhv_period=10, multiplier=2.5)
    assert len(res) == 15

    # First 4 bars: ATR is None
    for i in range(4):
        assert res[i].atr is None
        assert res[i].ready is False
        assert res[i].raw_line is None

    # Bar 5 (index 4): First ATR initialized (SMA of first 5 ranges)
    # Each bar has range = high - low = 20.0 (previous close was 105+(i-1), high 110+i -> diff 5-10, TR is 20)
    assert res[4].atr is not None
    assert pytest.approx(res[4].atr, 0.01) == 20.0
    assert res[4].ready is False  # HHV(10) not full yet

    # Bar 14 (index 13: 10th ATR bar): ready = True
    assert res[13].ready is True
    assert res[13].raw_line is not None
    # High is 110 + 13 = 123. ATR is ~20. Multiplier 2.5 -> stop ~ 123 - 50 = 73
    assert res[13].raw_line > 0


def test_ratchet_option_stop_lifecycle():
    orig_sl = 123.65

    # 1. Raw ATR line is below original option SL -> does NOT activate
    stop, mode, inc = ratchet_option_stop(None, 120.0, orig_sl, protection_mode="NONE")
    assert stop == 0.0
    assert mode == "NONE"
    assert inc is False

    # 2. Raw ATR line strictly above original option SL -> activates
    stop, mode, inc = ratchet_option_stop(None, 127.0, orig_sl, protection_mode="NONE")
    assert stop == 127.0
    assert mode == "ATR"
    assert inc is True

    # 3. Raw line falls -> locked stop does NOT fall
    stop2, mode2, inc2 = ratchet_option_stop(stop, 125.0, orig_sl, protection_mode="ATR")
    assert stop2 == 127.0
    assert mode2 == "ATR"
    assert inc2 is False

    # 4. Raw line rises -> locked stop ratchets higher
    stop3, mode3, inc3 = ratchet_option_stop(stop2, 135.5, orig_sl, protection_mode="ATR")
    assert stop3 == 135.5
    assert mode3 == "ATR"
    assert inc3 is True

    # 5. T2 fallback floor mode (ATR hasn't qualified by T2)
    stop_floor, mode_floor, inc_floor = ratchet_option_stop(
        None, 120.0, orig_sl, protection_mode="ORIGINAL_OPTION_SL_FLOOR"
    )
    assert stop_floor == 123.65
    assert mode_floor == "ORIGINAL_OPTION_SL_FLOOR"

    # Upgrades from floor to ATR once raw line exceeds original SL
    stop_up, mode_up, inc_up = ratchet_option_stop(
        stop_floor, 130.0, orig_sl, protection_mode="ORIGINAL_OPTION_SL_FLOOR"
    )
    assert stop_up == 130.0
    assert mode_up == "ATR"
    assert inc_up is True


def test_option_candle_properties():
    c = OptionCandle(
        start_time=datetime(2026, 10, 23, 9, 15, tzinfo=IST),
        end_time=datetime(2026, 10, 23, 9, 20, tzinfo=IST),
        open=100.0,
        high=105.0,
        low=95.0,
        close=102.0,
        volume=500.0,
    )
    assert c.start == c.start_time
    assert c.end == c.end_time
    assert c.timestamp == "2026-10-23T09:15:00+05:30"
    assert c.end_timestamp == "2026-10-23T09:20:00+05:30"


def test_parse_candle_edge_cases():
    # Missing timestamp
    assert parse_option_candle({"open": 100, "high": 110, "low": 90, "close": 105}) is None
    # Invalid timestamp format
    assert parse_option_candle({"timestamp": "not-a-date", "open": 100, "high": 110, "low": 90, "close": 105}) is None
    # Timezone naive timestamp gets IST timezone attached
    c = parse_option_candle({"timestamp": "2026-10-23 09:15:00", "open": 100, "high": 110, "low": 90, "close": 105})
    assert c is not None
    assert c.start_time.tzinfo is not None


def test_aggregate_minute_candles_edge_cases():
    # Empty
    assert aggregate_minute_candles_to_5m([]) == []
    # Observed at before bucket end
    obs = datetime(2026, 10, 23, 9, 18, tzinfo=IST)
    minute_bars = [
        {
            "timestamp": (datetime(2026, 10, 23, 9, 15, tzinfo=IST) + timedelta(minutes=i)).isoformat(),
            "open": 100.0,
            "high": 105.0,
            "low": 95.0,
            "close": 100.0,
            "volume": 10.0,
            "complete": True,
        }
        for i in range(5)
    ]
    # Filtered because observed_at (09:18) is before bucket end (09:20)
    assert aggregate_minute_candles_to_5m(minute_bars, observed_at=obs) == []
