"""Immutable, event-time Discord contract for option reference lifecycle."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from alerts import render_option_lifecycle_event, send_option_lifecycle_event


def event(kind="T2_HIT", milestone=2):
    return {
        "event_id": "trade:7:target2", "type": kind,
        "event_time": "2026-10-23T05:30:00Z", "index_price": 22472.1,
        "previous_stop": 148.15455043953523,
        "candle": {"start_time": "2026-10-23T10:55:00+05:30",
                   "end_time": "2026-10-23T11:00:00+05:30", "close": 147.5},
        "trade": {"id": "trade", "display_id": "1950", "direction": "BEARISH",
                  "entry_price": 22552.1, "stop_loss": 22552.1,
                  "target_1": 22527.1, "target_2": 22472.1},
        "option_trailing": {
            "policy_version": "option_atr_v1", "phase": "POST_T2", "highest_milestone": milestone,
            "booked_fraction": .8, "remaining_fraction": .2,
            "original_option_entry": 139.25, "original_option_sl": 123.65,
            "locked_option_sl": 152.70123456, "protection_mode": "ATR",
            "contract": {"security_id": "51333", "expiry": "2026-10-27", "strike": 22550,
                         "option_type": "PE", "exchange_segment": "NSE_FNO"},
        },
    }


@pytest.mark.parametrize("kind,card", [
    ("NEW_SIGNAL", "A"), ("T1_HIT", "B"), ("ATR_ACTIVE", "C"),
    ("OPTION_SL_RATCHETED", "D"), ("T2_HIT", "E"), ("SL_HIT", "F"),
    ("STOPPED_OUT_AT_BE", "G"), ("ATR_SL_HIT", "I"),
    ("OPTION_SL_FLOOR_HIT", "I"), ("MONITORING_UNAVAILABLE", "J"),
    ("MONITORING_RESTORED", "K"), ("PERSISTENCE_FAILURE", "L"),
])
def test_catalog_is_event_time_contract_and_unit_explicit(kind, card):
    snapshot = event(kind)
    original = deepcopy(snapshot)
    embed = render_option_lifecycle_event(snapshot)["embeds"][0]
    assert f"Card {card}" in embed["title"]
    for value in ("#1950", "2026-10-27", "51333", "11:00:00 IST", "POST_T2", "Reference/model"):
        assert value in str(embed)
    assert "Estimated Option SL" not in str(embed)
    assert "New SL" not in str(embed)
    assert snapshot == original


def test_exit_retains_target_booking_separate_from_exited_remainder():
    snapshot = event("ATR_SL_HIT")
    snapshot["option_trailing"].update(phase="CLOSED", booked_fraction=1, remaining_fraction=0)
    description = render_option_lifecycle_event(snapshot)["embeds"][0]["description"]
    for value in ("Targets model-booked: 80%", "Remainder closed: 20%", "Score: 2 retained",
                  "₹ 147.50", "₹ 148.15", "10:55:00 IST", "Index guard: disabled"):
        assert value in description
    assert "Targets model-booked: 100%" not in description


def test_pre_t2_exit_card_and_persistence_warning_do_not_claim_recorded_score():
    snapshot = event("ATR_SL_HIT", 1)
    snapshot["option_trailing"].update(phase="CLOSED", remaining_fraction=0)
    payload = render_option_lifecycle_event(snapshot)
    assert "Card H" in payload["embeds"][0]["title"]
    assert "Remainder closed: 50%" in str(payload)
    snapshot["type"] = "PERSISTENCE_FAILURE"
    payload = render_option_lifecycle_event(snapshot)
    assert "Database pending" in str(payload)
    assert "Score:" not in str(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [(None, "DELIVERED"), (httpx.ReadTimeout("unknown"), "AMBIGUOUS"),
                                           (httpx.ConnectError("not sent"), "FAILED")])
async def test_sender_reports_transport_outcome_without_mutating_snapshot(error, expected):
    snapshot = event()
    original = deepcopy(snapshot)
    client = AsyncMock()
    client.post.return_value = MagicMock()
    if error:
        client.post.side_effect = error
    with patch("alerts.settings.discord_webhook_url", "http://discord.invalid"), patch("alerts.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        assert await send_option_lifecycle_event(snapshot) == expected
    assert snapshot == original


@pytest.mark.parametrize("timestamp", [None, "2026-10-23T10:08:00", datetime(2026, 10, 23, 10, 8)])
def test_missing_or_naive_event_timestamp_is_unavailable_and_closed_failure_stays_closed(timestamp):
    snapshot = event("PERSISTENCE_FAILED")
    snapshot["event_time"] = timestamp
    snapshot["option_trailing"]["phase"] = "CLOSED"
    description = render_option_lifecycle_event(snapshot)["embeds"][0]["description"]
    assert "Event time: unavailable" in description
    assert "Lifecycle: CLOSED" in description
    assert "Score:" not in description


@pytest.mark.asyncio
@pytest.mark.parametrize("signal_timezone", [None, timezone(timedelta(hours=5, minutes=30)), timezone.utc])
async def test_initial_enrolled_signal_keeps_bracket_and_adds_card_a_protection(signal_timezone):
    from alerts import send_discord
    from fetchers.price_fetcher import PriceFetcher
    from types import SimpleNamespace
    from models import AresSignal, Direction, SetupType
    ist = timezone(timedelta(hours=5, minutes=30))
    with patch("fetchers.price_fetcher.dhanhq"):
        fetcher = PriceFetcher()
    fetcher.dhan.intraday_minute_data.return_value = {"status": "success", "data": {
        "timestamp": ["2026-10-23T04:38:00+00:00"],
        "open": [24000.], "high": [24000.], "low": [24000.], "close": [24000.], "volume": [100],
    }}
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 10, 23, 10, 9, tzinfo=ist)
        candle = await fetcher.fetch_latest_candle()
    assert candle.timestamp == datetime(2026, 10, 23, 10, 8)
    signal_timestamp = candle.timestamp
    if signal_timezone is not None:
        signal_timestamp = signal_timestamp.replace(tzinfo=ist).astimezone(signal_timezone)
    signal = AresSignal(setup_type=SetupType.FAILED_BREAKOUT, direction=Direction.BULLISH,
                        trigger_price=24000, entry_zone=(23999, 24001), stop_loss=23990,
                        target_1=24018, target_2=24040, confidence="HIGH", reasons=["Confirmed"],
                        timestamp=signal_timestamp, strike_to_trade=24000, option_type="CE")
    signal.bracket = SimpleNamespace(security_id="51333", expiry_date="2026-10-27", entry_limit=100.5,
        target_1=110., target_2=122., catastrophic_opt_sl=95.5, synthetic_spot_sl=23990,
        lots=2, greeks=SimpleNamespace(delta=.5, gamma=.002, theta=-15., vega=12., iv=12.5))
    signal.options_sizing_status = "VALID"
    signal.capital = 50000
    signal.risk_pct = 1
    signal.option_premium = 100.
    signal.market_context['option_trailing'] = event('NEW_SIGNAL', 0)['option_trailing']
    client = AsyncMock()
    client.post.return_value = MagicMock()
    with patch('alerts.settings.discord_webhook_url', 'http://discord.invalid'), patch('alerts.httpx.AsyncClient') as factory:
        factory.return_value.__aenter__.return_value = client
        assert await send_discord(signal, 24000)
    fields = client.post.call_args.kwargs['json']['embeds'][0]['fields']
    values = {field['name']: field['value'] for field in fields}
    assert values['🕒 Time'] == '23-Oct-2026 10:08:00 IST'
    assert signal.timestamp == signal_timestamp
    assert 'Card A' in str(fields)
    for text in ('2026-10-27', '51333', 'index points', 'after index T1', 'Reference/model', '100.50', '110.00'):
        assert text in str(fields)
