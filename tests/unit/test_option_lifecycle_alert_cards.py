"""Clean Discord alert contract for option reference lifecycle (no Cards)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from alerts import render_option_lifecycle_event, send_option_lifecycle_event, send_trade_update


def event(kind="T2_HIT", milestone=2):
    return {
        "event_id": "trade:7:target2", "type": kind,
        "event_time": "2026-10-23T05:30:00Z", "index_price": 22472.1,
        "previous_stop": 148.15455043953523,
        "candle": {"start_time": "2026-10-23T10:55:00+05:30",
                   "end_time": "2026-10-23T11:00:00+05:30", "close": 147.5},
        "trade": {"id": "trade", "display_id": "1950", "direction": "BEARISH",
                  "setup_type": "TREND_CONTINUATION",
                  "entry_price": 22552.1, "stop_loss": 22552.1,
                  "target_1": 22527.1, "target_2": 22472.1, "state": "OPEN",
                  "market_context": {
                      "options_sizing": {
                          "entry_limit": 139.25,
                          "target_1": 150.0,
                          "target_2": 170.0,
                          "lot_size": 25,
                          "lots": 1,
                      },
                      "option_trailing": {
                          "policy_version": "option_atr_v1", "phase": "POST_T2", "highest_milestone": milestone,
                          "booked_fraction": .8, "remaining_fraction": .2,
                          "original_option_entry": 139.25, "original_option_sl": 123.65,
                          "locked_option_sl": 152.70123456, "protection_mode": "ATR",
                          "contract": {"security_id": "51333", "expiry": "2026-10-27", "strike": 22550,
                                       "option_type": "PE", "exchange_segment": "NSE_FNO"},
                      },
                  }},
        "option_trailing": {
            "policy_version": "option_atr_v1", "phase": "POST_T2", "highest_milestone": milestone,
            "booked_fraction": .8, "remaining_fraction": .2,
            "original_option_entry": 139.25, "original_option_sl": 123.65,
            "locked_option_sl": 152.70123456, "protection_mode": "ATR",
            "contract": {"security_id": "51333", "expiry": "2026-10-27", "strike": 22550,
                         "option_type": "PE", "exchange_segment": "NSE_FNO"},
        },
    }


@pytest.mark.parametrize("kind", [
    "T1_HIT", "ATR_ACTIVE", "OPTION_SL_RATCHETED", "ATR_SL_UPDATED",
    "T2_HIT", "SL_HIT", "STOPPED_OUT_AT_BE", "ATR_SL_HIT",
    "OPTION_SL_FLOOR_HIT",
])
def test_clean_trade_update_format_has_no_cards_and_matches_spec(kind):
    snapshot = event(kind)
    original = deepcopy(snapshot)
    rendered = render_option_lifecycle_event(snapshot)
    embed = rendered["embeds"][0]
    title = embed["title"]
    description = embed["description"]

    # Must NEVER contain "Card A", "Card B", etc.
    assert "Card " not in title
    assert "Card " not in description

    # Standard title format
    assert "#1950 TRADE UPDATE: TREND_CONTINUATION (BEARISH)" in title

    # Correct status icons
    if kind in {"T1_HIT", "T2_HIT"}:
        assert "🎯 🟢" in title
    elif kind in {"SL_HIT", "STOPPED_OUT_AT_BE", "ATR_SL_HIT", "OPTION_SL_FLOOR_HIT"}:
        assert "💥 🛑" in title
    else:
        assert "🚨 🐻 🔴" in title

    # Standard body fields
    assert "🕒 **Time**" in description
    assert "📍 **Spot**" in description
    assert "⚡ **Action**" in description
    assert "✅ **Entry**" in description
    assert "🛑 **New SL**" in description
    assert "⭐ **Status**" in description

    # Specific option updates
    if kind in {"OPTION_SL_RATCHETED", "ATR_SL_UPDATED"}:
        assert "🛡️ **Active Option SL**: ₹ 152.70" in description

    # Immutability check
    assert snapshot == original


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["MONITORING_UNAVAILABLE", "MONITORING_RESTORED"])
async def test_monitoring_alerts_silenced_on_discord(kind):
    snapshot = event(kind)
    client = AsyncMock()
    with patch("alerts.settings.discord_webhook_url", "http://discord.invalid"), patch("alerts.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        outcome = await send_option_lifecycle_event(snapshot)
        assert outcome == "DELIVERED"
        # Must NOT dispatch HTTP post to Discord
        client.post.assert_not_called()


@pytest.mark.asyncio
async def test_send_trade_update_no_runtime_error_for_option_atr():
    trade = event("T1_HIT")["trade"]
    client = AsyncMock()
    with patch("alerts.settings.discord_webhook_url", "http://discord.invalid"), patch("alerts.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        # Must not raise RuntimeError
        await send_trade_update(trade, spot=22527.1, update_type="T1_HIT")
        client.post.assert_called_once()
        payload = client.post.call_args.kwargs["json"]
        assert "Card " not in payload["embeds"][0]["title"]
        assert "🎯 🟢 #1950 TRADE UPDATE: TREND_CONTINUATION (BEARISH)" in payload["embeds"][0]["title"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [(None, "DELIVERED"), (httpx.ReadTimeout("unknown"), "AMBIGUOUS"),
                                           (httpx.ConnectError("not sent"), "FAILED")])
async def test_sender_reports_transport_outcome_without_mutating_snapshot(error, expected):
    snapshot = event("T1_HIT")
    original = deepcopy(snapshot)
    client = AsyncMock()
    client.post.return_value = MagicMock()
    if error:
        client.post.side_effect = error
    with patch("alerts.settings.discord_webhook_url", "http://discord.invalid"), patch("alerts.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        assert await send_option_lifecycle_event(snapshot) == expected
    assert snapshot == original


@pytest.mark.asyncio
async def test_initial_enrolled_signal_keeps_bracket_without_card_a():
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
    signal_timestamp = candle.timestamp
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
    # Must NOT have Card A
    assert 'Card A' not in str(fields)
    # Must retain standard fields
    field_names = [f['name'] for f in fields]
    assert '📍 Spot' in field_names
    assert '⚡ Trade' in field_names
    assert '🛑 SL' in field_names
