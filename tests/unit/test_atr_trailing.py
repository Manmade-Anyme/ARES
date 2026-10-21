"""Public ATR aggregation and independent numeric regression examples."""
from datetime import datetime, timedelta, timezone

import pytest

from atr_trailing import completed_atr_bars


START = datetime.fromisoformat("2026-10-21T09:15:00+05:30")


def minutes(count=50, *, start=START, falling=False):
    rows = []
    for index in range(count):
        price = 200 - 10 * (index // 5) if falling else 100 + 10 * (index // 5)
        rows.append({"timestamp": (start + timedelta(minutes=index)).isoformat(),
                     "open": price, "high": price + 2, "low": price - 2,
                     "close": price, "complete": True})
    return rows


@pytest.mark.parametrize("falling, bullish, bearish", [
    (False, 163.31072, 126.68928),
    (True, 173.31072, 136.68928),
])
def test_wilder_seed_smoothing_and_ten_bar_extremes(falling, bullish, bearish):
    bars = completed_atr_bars(minutes(falling=falling))
    assert len(bars) == 10
    assert [bar["atr"] for bar in bars[:4]] == [None] * 4
    # TR=4 initially, then 12 for each gap. Seed=(4+12*4)/5=10.4.
    assert [bar["atr"] for bar in bars[4:]] == pytest.approx(
        [10.4, 10.72, 10.976, 11.1808, 11.34464, 11.475712]
    )
    assert all(bar["bullish_sl"] is None and bar["bearish_sl"] is None for bar in bars[:9])
    assert bars[-1]["bullish_sl"] == pytest.approx(bullish)
    assert bars[-1]["bearish_sl"] == pytest.approx(bearish)
    assert bars[-1]["timestamp"] == "2026-10-21T10:05:00+05:30"


def test_aggregation_uses_full_high_low_and_final_close():
    rows = minutes(5)
    rows[1]["high"] = 106
    rows[3]["low"] = 95
    rows[-1].update(close=101, high=103)
    bar = completed_atr_bars(rows)[0]
    assert bar == {"timestamp": "2026-10-21T09:20:00+05:30", "close": 101,
                   "atr": None, "bullish_sl": None, "bearish_sl": None}
    # Five identical aggregate ranges seed ATR=11, rather than last-minute 5.
    repeated = [dict(row, timestamp=(START + timedelta(minutes=offset + i)).isoformat())
                for offset in range(0, 25, 5) for i, row in enumerate(rows)]
    assert completed_atr_bars(repeated)[-1]["atr"] == 11


@pytest.mark.parametrize("count, expected", [(0, 0), (4, 0), (5, 1), (9, 1)])
def test_only_whole_completed_five_minute_bars(count, expected):
    assert len(completed_atr_bars(minutes(count))) == expected


@pytest.mark.parametrize("completion", [False, None, 1, "true"])
def test_forming_candle_does_not_become_complete_by_aging(completion):
    rows = minutes(15)
    rows[4]["complete"] = completion
    rows[4]["sampled_at"] = "2026-10-21T15:30:00+05:30"
    bars = completed_atr_bars(rows)
    assert [bar["timestamp"] for bar in bars] == [
        "2026-10-21T09:25:00+05:30", "2026-10-21T09:30:00+05:30"
    ]
    assert all(bar["atr"] is None for bar in bars)


def test_duplicate_latest_value_wins_and_output_is_chronological():
    rows = minutes(5)
    revised = dict(rows[-1], close=101)
    bars = completed_atr_bars(list(reversed(rows)) + [revised])
    assert bars[0]["close"] == 101
    assert completed_atr_bars(rows + [dict(revised, complete=False)]) == []
    assert completed_atr_bars(rows + [dict(revised, close="bad")]) == []


def test_no_lookahead_later_prices_do_not_change_prior_bars():
    rows = minutes(55)
    assert completed_atr_bars(rows)[:10] == completed_atr_bars(rows[:50])
    assert completed_atr_bars(rows[::-1]) == completed_atr_bars(rows)


def test_gap_resets_atr_and_extremes():
    rows = minutes(105)
    del rows[54]  # Remove final minute of the 10:05-10:10 bar.
    bars = completed_atr_bars(rows)
    assert len(bars) == 20
    assert bars[9]["bullish_sl"] is not None
    assert [bar["atr"] for bar in bars[10:14]] == [None] * 4
    assert bars[14]["atr"] == pytest.approx(10.4)
    assert all(bar["bullish_sl"] is None for bar in bars[10:19])
    assert bars[19]["bullish_sl"] == pytest.approx(273.31072)


def test_session_change_resets_previous_close_and_warmup():
    rows = minutes() + minutes(25, start=START + timedelta(days=1), falling=True)
    bars = completed_atr_bars(rows)
    assert bars[10]["atr"] is None
    assert bars[-1]["atr"] == pytest.approx(10.4)
    assert bars[-1]["bullish_sl"] is None


@pytest.mark.parametrize("change", [
    {"open": None}, {"high": "bad"}, {"low": float("nan")},
    {"close": float("inf")}, {"open": 0}, {"low": -1}, {"open": True},
    {"high": 99}, {"low": 101}, {"high": 97, "low": 98},
])
def test_invalid_price_revision_invalidates_aggregate(change):
    rows = minutes(5)
    assert completed_atr_bars(rows + [dict(rows[2], **change)]) == []


def test_missing_price_invalidates_aggregate():
    rows = minutes(5)
    del rows[2]["close"]
    assert completed_atr_bars(rows) == []


@pytest.mark.parametrize("timestamp", [None, "bad", 1e100])
def test_invalid_timestamps_are_ignored(timestamp):
    rows = minutes(5)
    assert completed_atr_bars(rows + [{"timestamp": timestamp}]) == completed_atr_bars(rows)


@pytest.mark.parametrize("timestamp", [
    "2026-10-21T09:14:00+05:30", "2026-10-21T15:30:00+05:30",
    "2026-10-21T09:15:01+05:30", "2026-10-21T09:15:00.000001+05:30",
])
def test_outside_session_and_non_minute_starts_are_ignored(timestamp):
    rows = minutes(5)
    assert completed_atr_bars(rows + [dict(rows[0], timestamp=timestamp)]) == completed_atr_bars(rows)


def test_last_session_bar_ends_at_1530():
    rows = minutes(5, start=START.replace(hour=15, minute=25))
    assert completed_atr_bars(rows)[0]["timestamp"] == "2026-10-21T15:30:00+05:30"


@pytest.mark.parametrize("encoding", ["utc", "naive", "epoch", "milliseconds"])
def test_timestamp_encodings_normalize_to_ist(encoding):
    rows = minutes(5)
    for row in rows:
        at = datetime.fromisoformat(row["timestamp"])
        if encoding == "utc":
            row["timestamp"] = at.astimezone(timezone.utc).isoformat()
        elif encoding == "naive":
            row["timestamp"] = at.replace(tzinfo=None).isoformat()
        else:
            row["timestamp"] = at.timestamp() * (1000 if encoding == "milliseconds" else 1)
    assert completed_atr_bars(rows) == completed_atr_bars(minutes(5))


def test_empty_and_non_mapping_input_rows():
    assert completed_atr_bars(None) == []
    assert completed_atr_bars([None, "bad", 123]) == []


def test_extremes_use_only_latest_ten_bars():
    rows = minutes(55)
    rows[0]["high"] = 1000
    assert completed_atr_bars(rows)[9]["bullish_sl"] > 500
    assert completed_atr_bars(rows)[10]["bullish_sl"] < 210
