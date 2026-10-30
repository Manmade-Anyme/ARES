"""Observable alert/report contracts for executable option brackets."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerts import format_signal, send_discord, send_trade_update
from models import AresSignal, Direction, SetupType


@pytest.mark.asyncio
async def test_t2_touch_card_reports_active_runner_without_option_exit():
    trade = {"direction": "BEARISH", "setup_type": "TREND_CONTINUATION", "display_id": 1950,
             "state": "T1_HIT", "entry_price": 22552.10, "stop_loss": 22546.46, "target_2": 22472.10,
             "market_context": {
                 "target_milestones": {"t2": {"touched_at": "2026-10-23T04:45:00+00:00", "time_basis": "candle_start", "target_spot": 22472.10,
                                                "spot_points_from_entry": 80.0}},
                 "options_sizing": {"entry_limit": 139.25, "target_2": 180, "lot_size": 65},
             }}
    client = AsyncMock()
    client.post.return_value = MagicMock()
    with patch("alerts.settings.discord_webhook_url", "http://example.invalid"), patch("httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        await send_trade_update(trade, 22472.10, "T2_TOUCHED")
    description = client.post.call_args.kwargs["json"]["embeds"][0]["description"]
    for text in ("Target 2 touched", "ATR runner remains active", "22472.10", "22552.10", "22546.46", "10:15:00", "IST"):
        assert text in description
    assert "Estimated Option Exit" not in description
    assert "Trade Closed" not in description
    assert "180.00" in description and "reference" in description
    assert "+80.00 points (reference)" in description
    assert "candle start; exact touch time unknown" in description
    assert "not an observed premium touch or executed exit" in description


@pytest.mark.asyncio
async def test_t2_touch_without_marker_time_does_not_invent_touch_timestamp():
    trade = {"direction": "BULLISH", "setup_type": "TREND_CONTINUATION", "state": "T1_HIT",
             "entry_price": 24000, "stop_loss": 24010, "target_2": 24100}
    client = AsyncMock()
    client.post.return_value = MagicMock()
    with patch("alerts.settings.discord_webhook_url", "http://example.invalid"), patch("httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        await send_trade_update(trade, 24100, "T2_TOUCHED")
    description = client.post.call_args.kwargs["json"]["embeds"][0]["description"]
    assert "**Time reference**: unavailable" in description
    assert "ATR runner remains active" in description
    assert "Estimated Option Exit" not in description


def signal_with_bracket():
    signal = AresSignal(
        setup_type=SetupType.FAILED_BREAKOUT, direction=Direction.BULLISH,
        trigger_price=24000, entry_zone=(23999, 24001), stop_loss=23990,
        target_1=24018, target_2=24040, confidence="HIGH", reasons=["Confirmed"],
        timestamp=datetime.now(), strike_to_trade=24000, option_type="CE",
    )
    signal.bracket = SimpleNamespace(
        is_valid=True, rejection_reason=None, security_id="42528", expiry_date="2026-10-20",
        entry_limit=100.5, synthetic_spot_sl=23990, catastrophic_opt_sl=95.5,
        target_1=110, target_2=122, lots=2,
        greeks=SimpleNamespace(delta=.5, gamma=.002, theta=-15, vega=12, iv=12.5),
    )
    signal.option_premium = 100
    signal.capital = 50000
    signal.risk_pct = 1
    signal.options_sizing_status = "VALID"
    return signal


def test_text_alert_contains_bracket_and_greeks():
    message = format_signal(signal_with_bracket(), 24000)
    for value in ("Option Entry", "100.50", "110.00", "122.00", "95.50",
                  "Layer 1", "Layer 2", "0.0020", "12.5%", "2 Lots", "42528"):
        assert value in message


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', [None, 'NO_AVAILABLE_CAPITAL', 'INSUFFICIENT_CAPITAL_OR_RISK_BUDGET', 'INVALID_BRACKET_ORDERING'])
@pytest.mark.parametrize('show_details', [False, True])
@pytest.mark.parametrize('lots', [0, 2])
async def test_execution_status_never_changes_visible_option_fields(reason, show_details, lots):
    signal = signal_with_bracket()
    signal.bracket.is_valid = reason is None
    signal.bracket.lots = lots
    signal.bracket.rejection_reason = reason
    signal.options_sizing_status = reason or 'VALID'
    signal.capital = 0 if reason == 'NO_AVAILABLE_CAPITAL' else 1
    message = format_signal(signal, 24000)
    for value in ('100.50', '95.50', '110.00', '122.00', f'{lots} Lots'):
        assert value in message
    for text in ('REJECTED', 'NON-EXECUTABLE', 'Reference only', 'lots planned', 'LIMIT', 'PAPER', 'No executable bracket'):
        assert text not in message
    if reason:
        assert reason not in message
    client = AsyncMock()
    client.post.return_value = MagicMock()
    with patch('alerts.settings.discord_show_option_details', show_details), patch('alerts.settings.discord_webhook_url', 'http://example.invalid'), patch('httpx.AsyncClient') as factory:
        factory.return_value.__aenter__.return_value = client
        assert await send_discord(signal, 24000)
    fields = {field['name']: field['value'] for field in client.post.call_args.kwargs['json']['embeds'][0]['fields']}
    assert '100.50' in fields['✅ Option Entry']
    assert fields['🛑 Stop Loss'] == '₹ 95.50'
    assert '110.00' in fields['🎯 Target 1']
    assert '122.00' in fields['🎯 Target 2']
    assert fields['📐 Sizing'] == f'{lots} Lots ({lots * 65} qty)'
    assert '⚠️ Option Bracket' not in fields
    for text in ('REJECTED', 'NON-EXECUTABLE', 'Reference only', 'lots planned', 'LIMIT', 'PAPER'):
        assert text not in str(fields)
    if reason:
        assert reason not in str(fields)
    assert '⚡ Order Type' not in fields
    for name in ('📊 Option Greeks', '💰 Risk Budget'):
        assert (name in fields) is show_details
    assert len(fields) <= 25


def test_invalid_sizing_with_missing_prices_formats_safely():
    signal = signal_with_bracket()
    signal.bracket = None
    signal.options_sizing_status = "INVALID_DATA"
    signal.suggested_lots = 0
    signal.option_premium = None
    assert "Option prices unavailable (INVALID_DATA)" in format_signal(signal, 24000)


@pytest.mark.asyncio
async def test_discord_invalid_bracket_does_not_advertise_executable_levels():
    signal = signal_with_bracket()
    signal.bracket.is_valid = False
    signal.bracket.rejection_reason = "MISSING_GREEKS"
    signal.bracket.greeks = None
    signal.options_sizing_status = "INVALID_DATA"
    client = AsyncMock()
    client.post.return_value = MagicMock()
    with patch("alerts.settings.discord_webhook_url", "http://example.invalid"), patch("httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        assert await send_discord(signal, 24000)
    fields = client.post.call_args.kwargs["json"]["embeds"][0]["fields"]
    text = str(fields)
    assert "MISSING_GREEKS" in text
    assert "LIMIT (Upfront Bracket)" not in text


@pytest.mark.asyncio
async def test_lifecycle_projection_is_not_claimed_as_broker_fill():
    trade = {
        "direction": "BULLISH", "setup_type": "FAILED_BREAKOUT", "state": "T1_HIT",
        "entry_price": 24000, "stop_loss": 24000,
        "market_context": {"options_sizing": {
            "entry_limit": 100, "target_1": 109.4, "target_2": 122,
            "lots": 2, "greeks": {"delta": .5, "gamma": .002},
        }},
    }
    client = AsyncMock()
    client.post.return_value = MagicMock()
    with patch("alerts.settings.discord_webhook_url", "http://example.invalid"), patch("httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        await send_trade_update(trade, 24018, "T1_HIT")
    description = client.post.call_args.kwargs["json"]["embeds"][0]["description"]
    assert "Estimated" in description
    assert "109.40" in description
    assert "not confirmed fills" in description
    assert "Realized" not in description


@pytest.mark.asyncio
@pytest.mark.parametrize('show_details', [False, True])
async def test_valid_discord_bracket_separates_limits_from_spot_levels(show_details):
    signal = signal_with_bracket()
    client = AsyncMock()
    client.post.return_value = MagicMock()
    with patch('alerts.settings.discord_show_option_details', show_details), patch("alerts.settings.discord_webhook_url", "http://example.invalid"), patch("httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        assert await send_discord(signal, 24000)
    fields = {field["name"]: field["value"] for field in client.post.call_args.kwargs["json"]["embeds"][0]["fields"]}
    assert "100.50" in fields["✅ Option Entry"]
    assert "LTP: ₹ 100.00" in fields["✅ Option Entry"]
    assert "24018.00" in fields["🎯 Target 1"]
    assert "122.00" in fields["🎯 Target 2"]
    assert fields["🛑 Stop Loss"] == "₹ 95.50"
    assert fields["🛑 SL"] == "**23990.00** (Spot Ref)"
    assert '⚡ Order Type' not in fields
    for name in ("📊 Option Greeks", "💰 Risk Budget"):
        assert (name in fields) is show_details
    if show_details:
        assert "Γ 0.0020" in fields["📊 Option Greeks"]
        assert "₹500" in fields["💰 Risk Budget"]
    assert len(fields) <= 25  # Discord embed field limit.


def test_discord_option_details_default_off_in_both_profiles():
    from config import Settings
    from config_profiles import EXPIRY_CONFIG, NON_EXPIRY_CONFIG

    settings = Settings()
    for profile in (EXPIRY_CONFIG, NON_EXPIRY_CONFIG):
        settings.apply_profile(profile)
        assert settings.discord_show_option_details is False


@pytest.mark.parametrize("update_type,spot,price", [
    ("T2_HIT", 24040, "122.00"), ("SL_HIT", 23990, "95.10"),
    ("STOPPED_OUT_AT_BE", 24000, "100.00"),
])
@pytest.mark.asyncio
async def test_other_lifecycle_prices_are_estimates(update_type, spot, price):
    trade = {
        "direction": "BULLISH", "setup_type": "FAILED_BREAKOUT", "state": "OPEN",
        "entry_price": 24000, "stop_loss": 23990,
        "market_context": {"options_sizing": {
            "entry_limit": 100, "target_1": 109.4, "target_2": 122,
            "final_lots": 2, "greeks": {"delta": .5, "gamma": .002},
        }},
    }
    client = AsyncMock()
    client.post.return_value = MagicMock()
    with patch("alerts.settings.discord_webhook_url", "http://example.invalid"), patch("httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        await send_trade_update(trade, spot, update_type)
    description = client.post.call_args.kwargs["json"]["embeds"][0]["description"]
    assert price in description
    assert "not confirmed fills" in description
    if update_type == "STOPPED_OUT_AT_BE":
        assert "Estimated Trade PnL" in description
        assert "+611.00" in description


def test_signal_quantity_uses_bracket_lot_size():
    signal = signal_with_bracket()
    signal.bracket.lot_size = 25
    assert "2 Lots (50 qty)" in format_signal(signal, 24000)


def test_signal_surfaces_iv_feed_warning_as_diagnostic_only():
    signal = signal_with_bracket()
    signal.bracket.iv_feed_warning = "IV_FEED_STALE"
    message = format_signal(signal, 24000)
    assert "IV_FEED_STALE" in message
    assert "diagnostic only" in message


@pytest.mark.asyncio
@pytest.mark.parametrize('update_type', ['ATR_SL_UPDATED', 'ATR_SL_HIT'])
@pytest.mark.parametrize('with_greeks', [True, False])
async def test_atr_lifecycle_discord_shows_stop_movement_and_close_trigger(update_type, with_greeks):
    trade = {
        'direction': 'BULLISH', 'setup_type': 'FAILED_BREAKOUT',
        'state': 'CLOSED' if update_type == 'ATR_SL_HIT' else 'T1_HIT',
        'entry_price': 24000, 'stop_loss': 24015,
        'market_context': {
            'atr_trailing': {'previous_sl': 24010},
            'options_sizing': {'entry_limit': 100, 'lot_size': 65,
                               'greeks': {'delta': .5, 'gamma': .002} if with_greeks else {}},
        },
    }
    client = AsyncMock()
    client.post.return_value = MagicMock()
    with patch('alerts.settings.discord_webhook_url', 'http://example.invalid'), patch('httpx.AsyncClient') as factory:
        factory.return_value.__aenter__.return_value = client
        await send_trade_update(trade, 24014, update_type)
    description = client.post.call_args.kwargs['json']['embeds'][0]['description']
    assert '24015.00' in description and '24010.00' in description
    assert '5-minute ATR trailing' in description
    if update_type == 'ATR_SL_UPDATED':
        assert 'Estimated Option SL' in description
        assert '107.72' in description if with_greeks else 'unavailable' in description
        assert 'Estimated Option Exit' not in description
    else:
        assert '5-minute close crossed ATR Stop Loss' in description
        assert 'not confirmed fills' in description


@pytest.mark.asyncio
@pytest.mark.parametrize('update_type', ['T1_HIT', 'ATR_SL_UPDATED', 'ATR_SL_HIT'])
async def test_manual_lifecycle_without_option_quote_does_not_invent_zero_premium(update_type):
    trade = {'direction': 'BEARISH', 'setup_type': 'TREND_CONTINUATION',
             'state': 'T1_HIT', 'entry_price': 24000, 'stop_loss': 23990,
             'market_context': {'options_sizing': {'entry_limit': 0, 'target_1': 0,
                                                   'option_target': 0, 'greeks': None}}}
    client = AsyncMock()
    client.post.return_value = MagicMock()
    with patch('alerts.settings.discord_webhook_url', 'http://example.invalid'), patch('httpx.AsyncClient') as factory:
        factory.return_value.__aenter__.return_value = client
        await send_trade_update(trade, 23980, update_type)
    description = client.post.call_args.kwargs['json']['embeds'][0]['description']
    assert '₹ unavailable' in description
    assert '₹ 0.00' not in description
