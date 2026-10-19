"""Regression coverage for completed exchange bars and full-session VWAP."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from fetchers.price_fetcher import PriceFetcher
from models import OHLCVCandle

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 19, 11, 34, 30, tzinfo=IST)


def response(times, prices=None, volumes=None):
    prices = prices or [100 + i * 10 for i in range(len(times))]
    return {"status": "success", "data": {
        "timestamp": times, "open": list(prices), "high": list(prices), "low": list(prices),
        "close": list(prices), "volume": volumes if volumes is not None else [10] * len(times),
    }}


def fetcher():
    with patch("fetchers.price_fetcher.dhanhq"):
        return PriceFetcher()


@pytest.mark.asyncio
async def test_completed_selection_late_start_full_session_vwap_and_watch_provenance():
    subject = fetcher()
    subject.dhan.intraday_minute_data.return_value = response(
        ["2026-10-19 09:15:00", "2026-10-19 11:33:00", "2026-10-19 11:34:00"],
        [100, 120, 900], [30, 10, 1000],
    )
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        result = await subject.fetch_latest_candle()
    assert result.timestamp == datetime(2026, 10, 19, 11, 33)
    assert result.vwap == 105
    assert subject.cumulative_vol == 40
    assert subject.watch_candles[-1]["complete"] is False


@pytest.mark.asyncio
async def test_duplicate_and_revised_provider_rows_do_not_double_count():
    subject = fetcher()
    subject.dhan.intraday_minute_data.side_effect = [
        response(["2026-10-19 09:15:00", "2026-10-19 11:33:00"], [100, 120]),
        response(["2026-10-19 09:15:00", "2026-10-19 11:33:00", "2026-10-19 11:33:00"], [110, 120, 130]),
        response(["2026-10-19 09:15:00", "2026-10-19 11:33:00", "2026-10-19 11:34:00"], [110, 130, 150]),
    ]
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        assert (await subject.fetch_latest_candle()).vwap == 110
        assert await subject.fetch_latest_candle() is None
        clock.now.return_value = NOW + timedelta(minutes=1)
        result = await subject.fetch_latest_candle()
    assert result.vwap == 130
    assert subject.cumulative_vol == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("times", [
    ["2026-10-19 11:34:00"], ["2026-10-16 11:33:00"],
    ["2026-10-19 11:35:00"],
    ["2026-10-19 09:14:00"], ["2026-10-19 15:30:00"],
])
async def test_partial_prior_session_future_or_invalid_rows_are_not_emitted(times):
    subject = fetcher()
    subject.dhan.intraday_minute_data.return_value = response(times)
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        assert await subject.fetch_latest_candle() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["seconds", "millis", "numeric_string", "naive", "offset"])
async def test_timestamp_formats_use_exchange_local_time(encoding):
    at = NOW.replace(minute=33, second=0)
    epoch = int(at.timestamp())
    stamp = {"seconds": epoch, "millis": epoch * 1000, "numeric_string": str(epoch),
             "naive": "2026-10-19 11:33:00", "offset": "2026-10-19T06:03:00+00:00"}[encoding]
    subject = fetcher()
    subject.dhan.intraday_minute_data.return_value = response([stamp])
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW.astimezone(timezone.utc)
        result = await subject.fetch_latest_candle()
    assert result.timestamp == at.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_reset_and_day_rollover_allow_fresh_bar_and_zero_volume_fallback():
    subject = fetcher()
    subject.dhan.intraday_minute_data.return_value = response(["2026-10-19 11:33:00"], [100], [0])
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        assert (await subject.fetch_latest_candle()).vwap == 100
        assert await subject.fetch_latest_candle() is None
        subject.reset_vwap()
        assert await subject.fetch_latest_candle() is not None
        clock.now.return_value = NOW + timedelta(days=1)
        subject.dhan.intraday_minute_data.return_value = response(["2026-10-20 09:15:00"], [110])
        result = await subject.fetch_latest_candle()
    assert result.vwap == 110
    assert subject.cumulative_vol == 10


@pytest.mark.asyncio
async def test_misaligned_arrays_do_not_emit_a_bar():
    subject = fetcher()
    payload = response(["2026-10-19 11:32:00", "2026-10-19 11:33:00"])
    payload["data"]["volume"] = [10]
    subject.dhan.intraday_minute_data.return_value = payload
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        with pytest.raises(ValueError, match="equal lengths"):
            await subject.fetch_latest_candle()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("volume", -1), ("close", float("nan")), ("high", 90)])
async def test_invalid_current_session_ohlcv_is_reported_as_provider_error(field, value):
    subject = fetcher()
    payload = response(["2026-10-19 11:33:00"], [100])
    payload["data"][field] = [value]
    subject.dhan.intraday_minute_data.return_value = payload
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        with pytest.raises(ValueError, match="OHLCV"):
            await subject.fetch_latest_candle()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("close", "bad-close"), ("close", float("nan")), ("open", True),
    ("high", 90), ("low", 110), ("volume", -1), ("volume", float("inf")),
])
@pytest.mark.parametrize("bad_index", [0, 1])
async def test_invalid_completed_row_with_other_valid_history_rejects_poll(field, value, bad_index):
    subject = fetcher()
    payload = response(["2026-10-19 09:15:00", "2026-10-19 11:33:00"], [100, 100])
    payload["data"][field][bad_index] = value
    subject.dhan.intraday_minute_data.return_value = payload
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        with pytest.raises(ValueError, match="OHLCV"):
            await subject.fetch_latest_candle()


@pytest.mark.asyncio
async def test_invalid_completed_duplicate_revision_cannot_fall_back_to_valid_duplicate():
    subject = fetcher()
    payload = response(["2026-10-19 11:33:00", "2026-10-19 11:33:00"], [100, 100])
    payload["data"]["close"][1] = "bad-close"
    subject.dhan.intraday_minute_data.return_value = payload
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        with pytest.raises(ValueError, match="OHLCV"):
            await subject.fetch_latest_candle()


@pytest.mark.asyncio
async def test_unknown_timestamp_mixed_with_valid_history_rejects_poll():
    subject = fetcher()
    subject.dhan.intraday_minute_data.return_value = response(
        ["2026-10-19 09:15:00", "unknown-bar-identity"])
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        with pytest.raises(ValueError, match="timestamp"):
            await subject.fetch_latest_candle()


@pytest.mark.asyncio
async def test_failed_new_session_response_preserves_previous_emission_and_vwap_state():
    subject = fetcher()
    first = response(["2026-10-19 11:33:00"], [100], [30])
    invalid = response(["2026-10-20 09:15:00", "2026-10-20 11:33:00"], [110, 120])
    invalid["data"]["close"][1] = "bad-close"
    subject.dhan.intraday_minute_data.side_effect = [first, invalid]
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        await subject.fetch_latest_candle()
        before = (subject._session_date, subject._last_emitted_timestamp,
                  subject.cumulative_tp_vol, subject.cumulative_vol, subject.watch_candles)
        clock.now.return_value = NOW + timedelta(days=1)
        with pytest.raises(ValueError, match="OHLCV"):
            await subject.fetch_latest_candle()
    assert (subject._session_date, subject._last_emitted_timestamp,
            subject.cumulative_tp_vol, subject.cumulative_vol, subject.watch_candles) == before


@pytest.mark.asyncio
async def test_invalid_forming_bar_does_not_reject_valid_completed_bar():
    subject = fetcher()
    payload = response(["2026-10-19 11:33:00", "2026-10-19 11:34:00"], [100, 110])
    payload["data"]["close"][1] = "unfinished-value"
    subject.dhan.intraday_minute_data.return_value = payload
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        assert (await subject.fetch_latest_candle()).close == 100


@pytest.mark.asyncio
async def test_watch_normalization_remains_forgiving_without_trading_strict_mode():
    from oi_watch_context import build_watch_candles
    payload = response(["2026-10-19 09:15:00", "2026-10-19 11:33:00"], [100, 100])
    payload["data"]["close"][1] = "bad-close"
    rows = build_watch_candles(payload["data"], NOW)
    assert len(rows) == 1
    assert rows[0]["timestamp"].startswith("2026-10-19T09:15:00")


@pytest.mark.asyncio
async def test_deferred_fetch_twice_before_ack_and_subsequent_duplicate_none():
    subject = fetcher()
    subject.dhan.intraday_minute_data.return_value = response(
        ["2026-10-19 09:15:00", "2026-10-19 11:33:00"], [100, 120]
    )
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        # First deferred fetch without auto_ack
        c1 = await subject.fetch_latest_candle(auto_ack=False)
        assert c1 is not None
        assert c1.timestamp == datetime(2026, 10, 19, 11, 33)

        # Second deferred fetch before ack returns the same candle
        c2 = await subject.fetch_latest_candle(auto_ack=False)
        assert c2 is not None
        assert c2.timestamp == c1.timestamp

        # Monotonically acknowledge candle consumption
        subject.acknowledge_candle(c1)

        # Once acknowledged, duplicate poll returns None
        assert await subject.fetch_latest_candle(auto_ack=False) is None
        assert await subject.fetch_latest_candle(auto_ack=True) is None


@pytest.mark.asyncio
async def test_acknowledge_candle_monotonic_marking():
    subject = fetcher()
    subject.dhan.intraday_minute_data.return_value = response(
        ["2026-10-19 11:32:00", "2026-10-19 11:33:00"])
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        current = await subject.fetch_latest_candle(auto_ack=False)
        subject.acknowledge_candle(current)
        subject.acknowledge_candle(current)
        subject.acknowledge_candle(OHLCVCandle(datetime(2026, 10, 19, 11, 32), 100, 100, 100, 100, 10))
        subject.acknowledge_candle(None)
        assert await subject.fetch_latest_candle(auto_ack=False) is None


@pytest.mark.parametrize("invalid", [
    None, datetime(2026, 10, 19, 11, 33),
    OHLCVCandle(datetime(2026, 10, 20, 11, 33), 100, 100, 100, 100, 10),
    OHLCVCandle(datetime(2026, 10, 16, 11, 33), 100, 100, 100, 100, 10),
    OHLCVCandle(datetime(2026, 10, 19, 8), 100, 100, 100, 100, 10),
    OHLCVCandle(datetime(2026, 10, 19, 15, 30), 100, 100, 100, 100, 10),
    OHLCVCandle(datetime(2026, 10, 19, 11, 34), 100, 100, 100, 100, 10),
])
@pytest.mark.asyncio
async def test_invalid_acknowledgement_cannot_suppress_valid_provider_bar(invalid):
    subject = fetcher()
    subject.dhan.intraday_minute_data.return_value = response(["2026-10-19 11:33:00"])
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        current = await subject.fetch_latest_candle(auto_ack=False)
        subject.acknowledge_candle(invalid)
        assert await subject.fetch_latest_candle(auto_ack=False) == current
        subject.acknowledge_candle(current)
        assert await subject.fetch_latest_candle(auto_ack=False) is None


@pytest.mark.asyncio
async def test_acknowledgement_before_any_source_session_cannot_suppress_valid_bar():
    subject = fetcher()
    subject.acknowledge_candle(OHLCVCandle(datetime(2026, 10, 20, 11, 33), 100, 100, 100, 100, 10))
    subject.dhan.intraday_minute_data.return_value = response(["2026-10-19 11:33:00"])
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        assert await subject.fetch_latest_candle(auto_ack=False) is not None
