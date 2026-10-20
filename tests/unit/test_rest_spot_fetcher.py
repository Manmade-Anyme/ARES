"""Fresh REST LTP is independent from completed detector candle data."""
from unittest.mock import patch

import pytest

from fetchers.price_fetcher import PriceFetcher


@pytest.fixture
def subject():
    with patch("fetchers.price_fetcher.dhanhq"):
        return PriceFetcher()


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_latest_spot_reads_exact_instrument_from_sdk_envelope(subject, nested):
    quotes = {"IDX_I": {"13": {"last_price": 24123.45}, "99": {"last_price": 999}}}
    subject.dhan.ticker_data.return_value = {
        "status": "success", "data": {"data": quotes, "status": "success"} if nested else quotes,
    }
    with patch("fetchers.price_fetcher.settings") as settings:
        settings.exchange_segment = "IDX_I"
        settings.security_id = "13"
        assert await subject.fetch_latest_spot() == 24123.45
    subject.dhan.ticker_data.assert_called_once_with({"IDX_I": [13]})
    subject.dhan.intraday_minute_data.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -10, True, None, "bad-price"])
async def test_invalid_rest_ltp_cannot_fall_back_to_candle(subject, value):
    subject.dhan.ticker_data.return_value = {"status": "success", "data": {"IDX_I": {"13": {"last_price": value}}}}
    with patch("fetchers.price_fetcher.settings") as settings:
        settings.exchange_segment = "IDX_I"
        settings.security_id = "13"
        with pytest.raises(ValueError):
            await subject.fetch_latest_spot()
    subject.dhan.intraday_minute_data.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"status": "failure", "data": {"IDX_I": {"13": {"last_price": 100}}}},
    {"status": "success", "data": {"IDX_I": {"99": {"last_price": 100}}}},
    {"status": "success", "data": {"NSE_EQ": {"13": {"last_price": 100}}}},
    {"status": "success", "data": {"IDX_I": {"13": {}}}},
    {"status": "success", "data": None}, None,
    {"status": "success", "data": {"status": "failure", "data": {"IDX_I": {"13": {"last_price": 100}}}}},
])
async def test_missing_or_failed_rest_quote_raises(subject, payload):
    subject.dhan.ticker_data.return_value = payload
    with patch("fetchers.price_fetcher.settings") as settings:
        settings.exchange_segment = "IDX_I"
        settings.security_id = "13"
        with pytest.raises(ValueError):
            await subject.fetch_latest_spot()
    subject.dhan.intraday_minute_data.assert_not_called()


@pytest.mark.asyncio
async def test_rest_transport_error_is_reported_without_candle_fallback(subject):
    subject.dhan.ticker_data.side_effect = RuntimeError("network unavailable")
    with patch("fetchers.price_fetcher.settings") as settings:
        settings.exchange_segment = "IDX_I"
        settings.security_id = "13"
        with pytest.raises(ValueError, match="request failed"):
            await subject.fetch_latest_spot()
    subject.dhan.intraday_minute_data.assert_not_called()
