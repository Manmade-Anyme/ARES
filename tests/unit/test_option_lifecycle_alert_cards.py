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
    if kind in {"ATR_ACTIVE", "OPTION_SL_RATCHETED", "ATR_SL_UPDATED"}:
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


def test_render_option_lifecycle_event_handles_none_index_price():
    """Missing or None index_price renders 'unavailable' without raising TypeError."""
    snapshot = event("ATR_ACTIVE")
    snapshot["index_price"] = None
    rendered = render_option_lifecycle_event(snapshot)
    desc = rendered["embeds"][0]["description"]
    assert "📍 **Spot**    : unavailable" in desc


def test_v2_quote_exit_uses_source_neutral_action_text():
    """V2 option premium exits don't falsely claim 5-minute candle closes."""
    # V2 - source neutral (POST_T1, milestone 1)
    snap_v2 = event("ATR_SL_HIT", milestone=1)
    snap_v2["option_trailing"]["policy_version"] = "option_premium_protection_v2"
    snap_v2["option_trailing"]["phase"] = "POST_T1"
    snap_v2["trade"]["market_context"]["option_trailing"]["policy_version"] = "option_premium_protection_v2"
    snap_v2["trade"]["market_context"]["option_trailing"]["phase"] = "POST_T1"
    rendered_v2 = render_option_lifecycle_event(snap_v2)
    desc_v2 = rendered_v2["embeds"][0]["description"]
    assert "Option Stop Loss Hit. Remaining 50% Closed (Score: 1 retained)." in desc_v2
    assert "5-minute close" not in desc_v2

    # V2 - source neutral (POST_T2, milestone 2)
    snap_v2_t2 = event("ATR_SL_HIT", milestone=2)
    snap_v2_t2["option_trailing"]["policy_version"] = "option_premium_protection_v2"
    snap_v2_t2["trade"]["market_context"]["option_trailing"]["policy_version"] = "option_premium_protection_v2"
    rendered_v2_t2 = render_option_lifecycle_event(snap_v2_t2)
    desc_v2_t2 = rendered_v2_t2["embeds"][0]["description"]
    assert "Option Stop Loss Hit. Final 20% Runner Closed (Score: 2 retained)." in desc_v2_t2
    assert "5-minute close" not in desc_v2_t2

    # V1 - preserves 5-minute candle wording
    snap_v1 = event("ATR_SL_HIT", milestone=1)
    snap_v1["option_trailing"]["policy_version"] = "option_atr_v1"
    snap_v1["option_trailing"]["phase"] = "POST_T1"
    snap_v1["trade"]["market_context"]["option_trailing"]["policy_version"] = "option_atr_v1"
    snap_v1["trade"]["market_context"]["option_trailing"]["phase"] = "POST_T1"
    rendered_v1 = render_option_lifecycle_event(snap_v1)
    desc_v1 = rendered_v1["embeds"][0]["description"]
    assert "Completed 5-minute close crossed Option ATR Stop. Remaining 50% Closed (Score: 1 retained)." in desc_v1


def test_atr_active_shows_locked_option_sl_and_no_exit_estimate():
    """ATR_ACTIVE shows active premium SL and does not display premature exit estimate."""
    snapshot = event("ATR_ACTIVE")
    rendered = render_option_lifecycle_event(snapshot)
    desc = rendered["embeds"][0]["description"]
    assert "🛡️ **Active Option SL**: ₹ 152.70" in desc
    assert "Estimated Option Exit" not in desc


def test_event_time_unavailable_is_preserved_for_invalid_historical_timestamps():
    """Invalid or naive timestamps on historical events render as 'unavailable', not now()."""
    snapshot = event("T1_HIT")
    snapshot["event_time"] = "invalid_timestamp"
    rendered = render_option_lifecycle_event(snapshot)
    desc = rendered["embeds"][0]["description"]
    assert "🕒 **Time**    : unavailable" in desc


def test_persistence_failed_alias_and_formatting():
    """PERSISTENCE_FAILED / PERSISTENCE_FAILURE show database warning and warning icon."""
    for kind in ("PERSISTENCE_FAILED", "PERSISTENCE_FAILURE"):
        snapshot = event(kind)
        rendered = render_option_lifecycle_event(snapshot)
        embed = rendered["embeds"][0]
        assert "⚠️" in embed["title"]
        assert embed["color"] == 15158332
        assert "Database persistence failure. Memory state active; unconfirmed in database." in embed["description"]
        assert "Trade Closed" not in embed["description"]


def test_stopped_out_at_be_post_t2_reports_runner_and_score_two():
    """Post-T2 break-even exit reports final 20% runner closed with score 2 retained."""
    snapshot = event("STOPPED_OUT_AT_BE", milestone=2)
    rendered = render_option_lifecycle_event(snapshot)
    desc = rendered["embeds"][0]["description"]
    assert "Final 20% Runner Closed (Score: 2 retained)." in desc


def test_option_estimate_uses_persisted_exit_leg_price():
    """When exit.option_close is missing, use accounting.legs.EXIT.option_price before falling back."""
    snapshot = event("STOPPED_OUT_AT_BE", milestone=1)
    snapshot["option_trailing"]["phase"] = "POST_T1"
    snapshot["trade"]["market_context"]["option_trailing"]["phase"] = "POST_T1"
    snapshot["option_trailing"]["accounting"] = {
        "legs": {
            "EXIT": {"option_price": 90.0}
        }
    }
    snapshot["trade"]["market_context"]["option_trailing"]["accounting"] = snapshot["option_trailing"]["accounting"]
    rendered = render_option_lifecycle_event(snapshot)
    desc = rendered["embeds"][0]["description"]
    # Entry is 139.25, exit should read 90.00 rather than falling back to 139.25 (₹0 pnl)
    assert "Estimated Option Exit**: ₹ 90.00" in desc


def test_t1_t2_estimate_prefers_persisted_target_leg_option_price():
    """T1/T2 alerts use actual captured leg option_price if present in accounting.legs."""
    snapshot = event("T1_HIT", milestone=1)
    snapshot["option_trailing"]["accounting"] = {
        "legs": {"T1": {"option_price": 165.50}}
    }
    snapshot["trade"]["market_context"]["option_trailing"]["accounting"] = snapshot["option_trailing"]["accounting"]
    rendered = render_option_lifecycle_event(snapshot)
    desc = rendered["embeds"][0]["description"]
    assert "Estimated Option Exit**: ₹ 165.50" in desc


def test_stopped_out_at_be_uses_option_total_points_for_versioned_trade():
    """Versioned trade at BE reports total PnL from accounting.option_total_points."""
    snapshot = event("STOPPED_OUT_AT_BE", milestone=1)
    snapshot["option_trailing"]["phase"] = "POST_T1"
    snapshot["trade"]["market_context"]["option_trailing"]["phase"] = "POST_T1"
    snapshot["option_trailing"]["accounting"] = {
        "option_total_points": -4.50,
        "legs": {"EXIT": {"option_price": 130.25}}
    }
    snapshot["trade"]["market_context"]["option_trailing"]["accounting"] = snapshot["option_trailing"]["accounting"]
    rendered = render_option_lifecycle_event(snapshot)
    desc = rendered["embeds"][0]["description"]
    # 1 lot of 25 * -4.50 = -112.50
    assert "Estimated Trade PnL**: ₹ -112.50 (reference accounting)" in desc


def test_t1_t2_versioned_alerts_never_show_projected_targets_and_use_stale_quote_up_to_30s():
    """Versioned T1/T2 alerts use <=30s last_option_quote marked '(stale quote)' or 'unavailable', never sizing.target_1/2."""
    for kind, milestone, bad_target in [("T1_HIT", 1, "150.00"), ("T2_HIT", 2, "170.00")]:
        event_at = datetime(2026, 10, 23, 11, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        snap_stale = event(kind, milestone=milestone)
        snap_stale["event_time"] = event_at.isoformat()
        snap_stale["option_trailing"]["last_option_quote"] = {
            "price": 146.50,
            "observed_at": (event_at - timedelta(seconds=25)).isoformat(),
            "source": "dhan_last_trade",
        }
        snap_stale["trade"]["market_context"]["option_trailing"]["last_option_quote"] = snap_stale["option_trailing"]["last_option_quote"]
        desc_stale = render_option_lifecycle_event(snap_stale)["embeds"][0]["description"]
        assert "Estimated Option Exit**: ₹ 146.50 (₹ +181.25 / lot) (stale quote)" in desc_stale
        assert bad_target not in desc_stale

        snap_expired = deepcopy(snap_stale)
        expired_quote = {
            "price": 146.50,
            "observed_at": (event_at - timedelta(seconds=35)).isoformat(),
            "source": "dhan_last_trade",
        }
        snap_expired["option_trailing"]["last_option_quote"] = expired_quote
        snap_expired["trade"]["market_context"]["option_trailing"]["last_option_quote"] = expired_quote
        desc_expired = render_option_lifecycle_event(snap_expired)["embeds"][0]["description"]
        assert "Estimated Option Exit**: ₹ unavailable (₹ unavailable / lot)" in desc_expired
        assert bad_target not in desc_expired


def test_closed_multi_leg_alerts_compute_1_lot_when_suggested_lots_zero_and_scale_when_positive():
    """When suggested/final lots is 0, compute Estimated Trade PnL on 1 lot; when >0, multiply by lots."""
    for kind in ("STOPPED_OUT_AT_BE", "ATR_SL_HIT"):
        snap_zero = event(kind, milestone=1)
        snap_zero["option_trailing"]["phase"] = "CLOSED"
        snap_zero["trade"]["market_context"]["option_trailing"]["phase"] = "CLOSED"
        snap_zero["trade"]["market_context"]["options_sizing"].update(lot_size=65, lots=0)
        snap_zero["option_trailing"]["accounting"] = {
            "option_total_points": 2.225,
            "legs": {"EXIT": {"option_price": 231.25}},
        }
        snap_zero["trade"]["market_context"]["option_trailing"]["accounting"] = snap_zero["option_trailing"]["accounting"]
        desc_zero = render_option_lifecycle_event(snap_zero)["embeds"][0]["description"]
        assert "Estimated Trade PnL**: ₹ +144.62 (1-lot reference accounting; 0 lots suggested)" in desc_zero

        snap_multi = deepcopy(snap_zero)
        snap_multi["trade"]["market_context"]["options_sizing"]["lots"] = 3
        desc_multi = render_option_lifecycle_event(snap_multi)["embeds"][0]["description"]
        assert "Estimated Trade PnL**: ₹ +433.88 (reference accounting; 3 lots)" in desc_multi


def test_reference_pnl_lots_and_stale_quote_edge_cases():
    """Cover final_lots=None fallbacks, missing lot keys, and stale-quote guard branches."""
    # final_lots=None falls back to lots=2, lots=0, or generic 1-lot accounting
    base = event("STOPPED_OUT_AT_BE", milestone=1)
    base["option_trailing"]["phase"] = "CLOSED"
    base["trade"]["market_context"]["option_trailing"]["phase"] = "CLOSED"
    base["option_trailing"]["accounting"] = {"option_total_points": 2.225, "legs": {"EXIT": {"option_price": 231.25}}}
    base["trade"]["market_context"]["option_trailing"]["accounting"] = base["option_trailing"]["accounting"]

    snap_two = deepcopy(base)
    snap_two["trade"]["market_context"]["options_sizing"] = {"lot_size": 65, "final_lots": None, "lots": 2}
    assert "₹ +289.25 (reference accounting; 2 lots)" in render_option_lifecycle_event(snap_two)["embeds"][0]["description"]

    snap_zero = deepcopy(base)
    snap_zero["trade"]["market_context"]["options_sizing"] = {"lot_size": 65, "final_lots": None, "lots": 0}
    assert "₹ +144.62 (1-lot reference accounting; 0 lots suggested)" in render_option_lifecycle_event(snap_zero)["embeds"][0]["description"]

    snap_missing = deepcopy(base)
    snap_missing["trade"]["market_context"]["options_sizing"] = {"lot_size": 65}
    assert "₹ +144.62 (1-lot reference accounting)" in render_option_lifecycle_event(snap_missing)["embeds"][0]["description"]

    # Stale quote guard branches: zero price, naive timestamp, invalid event_time, and None event_time wall fallback
    t1_base = event("T1_HIT", milestone=1)
    t1_base.pop("event_time", None)
    t1_base["option_trailing"].pop("t1_at", None)
    t1_base["trade"]["market_context"]["option_trailing"].pop("t1_at", None)
    now_iso = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()

    for bad_quote, ev_time in [
        ({"price": 0, "observed_at": now_iso}, "2026-10-23T11:00:00+05:30"),
        ({"price": 146.50, "observed_at": "2026-10-23T11:00:00"}, "2026-10-23T11:00:00+05:30"),
        ({"price": 146.50, "observed_at": now_iso}, "not-a-timestamp"),
    ]:
        snap = deepcopy(t1_base)
        snap["event_time"] = ev_time
        snap["option_trailing"]["last_option_quote"] = bad_quote
        snap["trade"]["market_context"]["option_trailing"]["last_option_quote"] = bad_quote
        desc = render_option_lifecycle_event(snap)["embeds"][0]["description"]
        assert "Estimated Option Exit**: ₹ unavailable (₹ unavailable / lot)" in desc

    snap_wall = deepcopy(t1_base)
    snap_wall["option_trailing"]["last_option_quote"] = {"price": 146.50, "observed_at": now_iso}
    snap_wall["trade"]["market_context"]["option_trailing"]["last_option_quote"] = snap_wall["option_trailing"]["last_option_quote"]
    desc_wall = render_option_lifecycle_event(snap_wall)["embeds"][0]["description"]
    assert "Estimated Option Exit**: ₹ 146.50 (₹ +181.25 / lot) (stale quote)" in desc_wall
