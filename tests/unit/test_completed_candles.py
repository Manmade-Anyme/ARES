"""Regression coverage for completed exchange bars and full-session VWAP."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from fetchers.price_fetcher import PriceFetcher

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 19, 11, 34, 30, tzinfo=IST)


def response(times, prices=None, volumes=None):
    prices = prices or [100 + i * 10 for i in range(len(times))]
    return {"status": "success", "data": {
        "timestamp": times, "open": prices, "high": prices, "low": prices,
        "close": prices, "volume": volumes if volumes is not None else [10] * len(times),
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
