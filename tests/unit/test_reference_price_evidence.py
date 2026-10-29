from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from execution.reference_prices import normalize_option_minutes, option_target_evidence, spot_exit_evidence
from fetchers.price_fetcher import PriceFetcher
from config import settings

IST = timezone(timedelta(hours=5, minutes=30))
START = datetime(2026, 10, 23, 10, 30, tzinfo=IST)


def minute(start=START, *, close=105, sampled=None, complete=True):
    return {"timestamp": start.isoformat(), "sampled_at": (sampled or start + timedelta(minutes=1)).isoformat(),
            "open": 100, "high": 110, "low": 90, "close": close, "complete": complete}


def quote(at, price):
    return {"observed_at": at.isoformat(), "price": price, "source": "test_quote"}


def test_exact_target_chooses_nearest_timestamped_observation_within_five_seconds():
    event = START + timedelta(seconds=30)
    rows = [quote(event - timedelta(seconds=4), 102), quote(event + timedelta(seconds=2), 103),
            quote(event + timedelta(seconds=6), 104)]
    evidence = option_target_evidence(event, observations=rows, option_minutes=[minute()], observed_at=START + timedelta(minutes=2))
    assert evidence == {"price": 103.0, "observed_at": (event + timedelta(seconds=2)).isoformat(),
                        "source": "test_quote", "approximate": False}
    assert option_target_evidence(event, observations=[rows[2]], observed_at=START + timedelta(minutes=2)) is None
    assert option_target_evidence(event, observations=[rows[1]], observed_at=event) is None


def test_wick_target_uses_same_minute_close_never_nearby_quote():
    event = START + timedelta(seconds=30)
    evidence = option_target_evidence(None, source_minute=START, observations=[quote(event, 108)],
                                      option_minutes=[minute(), minute(START + timedelta(minutes=1), close=109)],
                                      observed_at=START + timedelta(minutes=2))
    assert evidence == {"price": 105.0, "observed_at": (START + timedelta(minutes=1)).isoformat(),
                        "source": "completed_1m_close", "approximate": True}
    assert option_target_evidence(None, source_minute=START, option_minutes=[minute()], observed_at=event) is None
    assert option_target_evidence(event, source_minute=START + timedelta(minutes=1), option_minutes=[minute()],
                                  observed_at=START + timedelta(minutes=2)) is None


def test_forming_or_invalid_revision_cannot_be_aged_into_evidence():
    event = START + timedelta(seconds=30)
    later = START + timedelta(minutes=2)
    forming = minute(sampled=event, complete=False)
    assert option_target_evidence(event, option_minutes=[forming], observed_at=later) is None
    invalid = minute(close=120)
    assert option_target_evidence(event, option_minutes=[minute(), invalid], observed_at=later) is None
    assert option_target_evidence(event, option_minutes=[minute(sampled=later)], observed_at=START + timedelta(minutes=1)) is None


def test_provider_malformed_and_later_invalid_duplicate_are_unavailable():
    data = {"timestamp": [START.isoformat(), START.isoformat()], "open": [100, 100],
            "high": [110, 90], "low": [90, 90], "close": [105, 105], "volume": [1, 1]}
    rows = normalize_option_minutes(data, START.date(), START + timedelta(minutes=2))
    assert len(rows) == 1
    assert option_target_evidence(None, source_minute=START, option_minutes=rows,
                                  observed_at=START + timedelta(minutes=2)) is None
    data["close"].pop()
    assert normalize_option_minutes(data, START.date(), START + timedelta(minutes=2)) == []


def test_atr_exit_uses_minute_ending_at_close_then_quote_fallback():
    exit_at = START + timedelta(minutes=1)
    later = exit_at + timedelta(seconds=10)
    evidence = spot_exit_evidence(exit_at, spot_minutes=[minute(), minute(exit_at, close=106)],
                                  observations=[quote(exit_at, 999)], observed_at=later)
    assert evidence["price"] == 105 and evidence["observed_at"] == exit_at.isoformat()
    evidence = spot_exit_evidence(exit_at, observations=[quote(exit_at - timedelta(seconds=5), 98)], observed_at=later)
    assert evidence["price"] == 98 and evidence["approximate"] is False
    assert spot_exit_evidence(exit_at, observations=[quote(exit_at - timedelta(seconds=6), 98)], observed_at=later) is None


@pytest.mark.asyncio
async def test_pinned_history_request_validates_rows_and_returns_provider_provenance():
    fetcher = PriceFetcher.__new__(PriceFetcher)
    provider = Mock(return_value={"status": "success", "data": {"timestamp": [START.isoformat(), (START + timedelta(minutes=1)).isoformat()],
                       "open": [100, 100], "high": [110, 110], "low": [90, 90], "close": [105, 106], "volume": [1, 1]}})
    fetcher.dhan = SimpleNamespace(intraday_minute_data=provider)
    contract = dict(security_id="53796", exchange_segment="NSE_FNO", expiry="2026-11-03", strike=22850, option_type="CE")
    now = START + timedelta(minutes=1, seconds=5)
    rows = await fetcher.fetch_option_minutes(contract, START, observed_at=now)
    provider.assert_called_once_with("53796", "NSE_FNO", "OPTIDX", "2026-10-23", "2026-10-23", interval=1)
    assert rows[0]["complete"] is True and rows[1]["complete"] is True
    assert datetime.fromisoformat(rows[0]["sampled_at"]) >= now
    assert option_target_evidence(None, source_minute=START, option_minutes=rows, observed_at=datetime.now(timezone.utc))["price"] == 105
    forming = normalize_option_minutes(provider.return_value["data"], START.date(), now)
    assert forming[0]["complete"] is True and forming[1]["complete"] is False
    for patch in ({"expiry": "2026-10-22"}, {"strike": 0}, {"option_type": "XX"}, {"security_id": "0"}):
        with pytest.raises(ValueError):
            await fetcher.fetch_option_minutes({**contract, **patch}, START, observed_at=now)
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_spot_history_recovers_exit_close_after_restart():
    fetcher = PriceFetcher.__new__(PriceFetcher)
    provider = Mock(return_value={"status": "success", "data": {"timestamp": [START.isoformat()],
                    "open": [100], "high": [110], "low": [90], "close": [105], "volume": [1]}})
    fetcher.dhan = SimpleNamespace(intraday_minute_data=provider)
    exit_at = START + timedelta(minutes=1)
    rows = await fetcher.fetch_spot_minutes(exit_at)
    provider.assert_called_once_with(settings.security_id, settings.exchange_segment, settings.instrument_type,
                                     "2026-10-23", "2026-10-23", interval=1)
    evidence = spot_exit_evidence(exit_at, spot_minutes=rows)
    assert evidence["price"] == 105
    assert evidence["observed_at"] == exit_at.isoformat()
    assert datetime.fromisoformat(rows[0]["sampled_at"]) >= exit_at


@pytest.mark.asyncio
async def test_spot_history_rejects_future_and_failure():
    fetcher = PriceFetcher.__new__(PriceFetcher)
    provider = Mock(return_value={"status": "failure"})
    fetcher.dhan = SimpleNamespace(intraday_minute_data=provider)
    assert await fetcher.fetch_spot_minutes(datetime.now(timezone.utc) + timedelta(days=1)) == []
    provider.assert_not_called()
    with pytest.raises(ValueError, match="Spot minute history request failed"):
        await fetcher.fetch_spot_minutes(START)


@pytest.mark.asyncio
async def test_pinned_quote_requires_fresh_provider_trade_timestamp():
    fetcher = PriceFetcher.__new__(PriceFetcher)
    now = datetime.now(timezone.utc)
    payload = {"last_price": 150, "last_trade_time": now.isoformat()}
    provider = Mock(return_value={"status": "success", "data": {"NSE_FNO": {"53796": payload}}})
    fetcher.dhan = SimpleNamespace(quote_data=provider)
    contract = dict(security_id="53796", exchange_segment="NSE_FNO", expiry="2099-11-03", strike=22850, option_type="PE")
    result = await fetcher.fetch_option_observation(contract)
    provider.assert_called_once_with({"NSE_FNO": [53796]})
    assert result["price"] == 150 and result["source"] == "dhan_last_trade"
    assert datetime.fromisoformat(result["observed_at"]) == now
    # Avoid waiting for the rate limit in this test; production calls are gated.
    fetcher._reference_next_request = 0
    payload["last_trade_time"] = (now - timedelta(seconds=20)).isoformat()
    assert await fetcher.fetch_option_observation(contract) is None
    fetcher._reference_next_request = 0
    del payload["last_trade_time"]
    assert await fetcher.fetch_option_observation(contract) is None
    fetcher._reference_next_request = 0
    payload["last_trade_time"] = datetime.now(IST).strftime("%d/%m/%Y %H:%M:%S")
    assert (await fetcher.fetch_option_observation(contract))["source"] == "dhan_last_trade"


def test_quote_timezone_and_provider_epoch_match_same_event():
    event = START.astimezone(timezone.utc)
    observation = quote(START + timedelta(seconds=5), 107)
    evidence = option_target_evidence(event, observations=[observation], observed_at=event + timedelta(seconds=5))
    assert evidence["price"] == 107
    observation["observed_at"] = (START + timedelta(seconds=6)).timestamp()
    assert option_target_evidence(event, observations=[observation], observed_at=event + timedelta(seconds=6)) is None
