"""Deterministic incident replay tests for #1950 and #3935.
Loads recorded fixtures from 2026-10-23 and simulates full chronological lifecycle.
"""
import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from models import AresSignal, SetupType, Direction, OptionTrailingContract, OptionTrailingContext
from position_manager import PositionManager
from options_candles import aggregate_to_5min, OptionCandle
from alerts import send_trade_update

IST = timezone(timedelta(hours=5, minutes=30))
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.mark.asyncio
async def test_replay_incident_1950():
    """Deterministic replay of incident #1950:
    Trade UUID: efbd19ae-b4d2-5e7a-a610-74251d2b3218
    Signal: 3ae913f6-084d-441f-967d-1e843326f3f1 (BEARISH 22550 PE, contract 51333)
    Index Entry: 22552.10, SL: 22577.10, T1: 22527.10, T2: 22472.10
    Option Entry: 139.25, SL: 123.65
    """
    option_file = FIXTURES_DIR / "ares-20261023-option-51333.json"
    with open(option_file, "r") as f:
        raw_option_1m = json.load(f)

    # 1. Aggregate 1m option candles to 5m completed bars
    five_min_bars = aggregate_to_5min(raw_option_1m, expected_security_id="51333")
    assert len(five_min_bars) >= 25, f"Expected at least 25 5m bars, got {len(five_min_bars)}"

    # 2. Setup Trade #1950
    pm = PositionManager()
    pm.is_initialized = True

    contract = OptionTrailingContract(
        security_id="51333",
        exchange_segment="NSE_FNO",
        expiry="2026-10-29",
        strike=22550,
        option_type="PE",
    )
    opt_ctx = OptionTrailingContext(
        policy_version="option_atr_v1",
        revision=1,
        contract=contract,
        phase="PRE_T1",
        highest_milestone=0,
        booked_fraction=0.0,
        remaining_fraction=1.0,
        original_option_sl=123.65,
        original_option_entry=139.25,
        index_guard_enabled=True,
        index_guard_stop=22552.10,
    )
    trade = {
        "id": "efbd19ae-b4d2-5e7a-a610-74251d2b3218",
        "signal_id": "3ae913f6-084d-441f-967d-1e843326f3f1",
        "display_id": "1950",
        "setup_type": "OI_WALL_REJECTION",
        "direction": "BEARISH",
        "entry_price": 22552.10,
        "stop_loss": 22577.10,
        "target_1": 22527.10,
        "target_2": 22472.10,
        "state": "OPEN",
        "entry_timestamp": "2026-10-23T04:00:00+00:00",
        "market_context": {
            "option_trailing": opt_ctx.to_dict(),
        },
    }
    pm.active_trades = [trade]

    rendered_embeds = []

    async def mock_send(t, spot, update_type):
        from alerts import trade_update_action_text, _option_lifecycle_estimate
        action = trade_update_action_text(update_type, t)
        est = _option_lifecycle_estimate(t, spot, update_type)
        rendered_embeds.append((update_type, action, est))

    with patch("position_manager.send_trade_update", side_effect=mock_send), \
         patch.object(pm.analytics, "apply_option_lifecycle_transition") as mock_apply:

        # Step 1: Pre-T1 spot touches 22540.00 (still in trade, SL not hit, T1 not hit)
        events = await pm.update_trades(spot_price=22540.00)
        assert len(events) == 0
        assert trade["state"] == "OPEN"
        assert trade["market_context"]["option_trailing"]["phase"] == "PRE_T1"

        # Step 2: Index hits T1 (22525.00) at 09:30 IST
        t1_time = datetime(2026, 10, 23, 9, 30, tzinfo=IST)
        events = await pm.update_trades(
            spot_price=22525.00,
            candle_low=22525.00,
            candle_high=22545.00,
            candle_timestamp=t1_time,
        )
        assert ("efbd19ae-b4d2-5e7a-a610-74251d2b3218", "T1_HIT") in events
        assert trade["state"] == "T1_HIT"
        assert trade["stop_loss"] == 22552.10  # Index BE stop active
        opt = trade["market_context"]["option_trailing"]
        assert opt["phase"] == "POST_T1"
        assert opt["highest_milestone"] == 1
        assert opt["booked_fraction"] == 0.5
        assert opt["remaining_fraction"] == 0.5

        # Check rendered T1 card (Card B)
        assert any(e[0] == "T1_HIT" and "Target 1 Reached! 50% model-booked (Score: 1)" in e[1] for e in rendered_embeds)

        # Step 3: Stream initial option candles (bars 0 to 15, from 09:15 to 10:30)
        # Indicator calculates Wilder ATR(5) and HHV(10) of (High - 2.5 * ATR5)
        early_bars = five_min_bars[:16]
        events_opt = await pm.update_option_atr_trailing(early_bars)
        # Option ATR line ratchets upward
        opt = trade["market_context"]["option_trailing"]
        assert opt["locked_option_sl"] is not None
        assert opt["locked_option_sl"] > 123.65  # Strictly higher than original option SL
        assert opt["protection_mode"] == "ATR"

        # Step 4: Index hits T2 (22470.00) at 10:35 IST
        t2_time = datetime(2026, 10, 23, 10, 35, tzinfo=IST)
        events_t2 = await pm.update_trades(
            spot_price=22470.00,
            candle_low=22470.00,
            candle_high=22510.00,
            candle_timestamp=t2_time,
        )
        assert ("efbd19ae-b4d2-5e7a-a610-74251d2b3218", "T2_HIT") in events_t2
        assert trade["state"] == "T2_REACHED"
        opt = trade["market_context"]["option_trailing"]
        assert opt["phase"] == "POST_T2"
        assert opt["highest_milestone"] == 2
        assert opt["booked_fraction"] == 0.8
        assert opt["remaining_fraction"] == 0.2
        assert opt["index_guard_enabled"] is False  # Index SL disabled!

        # Check rendered T2 card (Card E)
        assert any(e[0] == "T2_HIT" and "Cumulative 80% model-booked (Score: 2)" in e[1] for e in rendered_embeds)

        # Step 5: Later option candles (e.g. up to 11:45 where option price pulls back)
        # Simulate completed bar closing strictly below locked option SL
        current_locked = opt["locked_option_sl"]
        exit_bar_start = datetime(2026, 10, 23, 11, 45, tzinfo=IST)
        exit_bar = OptionCandle(
            start_time=exit_bar_start,
            end_time=exit_bar_start + timedelta(minutes=5),
            open=current_locked - 0.5,
            high=current_locked + 1.0,
            low=current_locked - 5.0,
            close=current_locked - 2.0,  # Closes strictly below locked stop
            volume=500000.0,
            security_id="51333",
            complete=True,
        )
        events_exit = await pm.update_option_atr_trailing([*early_bars, exit_bar])
        assert ("efbd19ae-b4d2-5e7a-a610-74251d2b3218", "ATR_SL_HIT") in events_exit
        assert trade["state"] == "CLOSED"
        opt = trade["market_context"]["option_trailing"]
        assert opt["phase"] == "CLOSED"
        assert opt["highest_milestone"] == 2  # Score 2 retained!
        assert opt["booked_fraction"] == 1.0
        assert opt["remaining_fraction"] == 0.0

        # Check rendered Runner Exit card (Card I)
        assert any(
            e[0] == "ATR_SL_HIT" and "Final 20% Runner Closed (Score: 2 retained)" in e[1]
            for e in rendered_embeds
        )


@pytest.mark.asyncio
async def test_replay_incident_3935():
    """Deterministic replay of incident #3935:
    Pre-T1 index SL hit closes 100% at score 0, option ATR never activated.
    """
    pm = PositionManager()
    pm.is_initialized = True

    contract = OptionTrailingContract(
        security_id="51333",
        exchange_segment="NSE_FNO",
        expiry="2026-10-29",
        strike=22550,
        option_type="PE",
    )
    opt_ctx = OptionTrailingContext(
        policy_version="option_atr_v1",
        revision=1,
        contract=contract,
        phase="PRE_T1",
        highest_milestone=0,
        booked_fraction=0.0,
        remaining_fraction=1.0,
        original_option_sl=123.65,
        original_option_entry=139.25,
        index_guard_enabled=True,
        index_guard_stop=22552.10,
    )
    trade = {
        "id": "trade-3935",
        "signal_id": "sig-3935",
        "display_id": "3935",
        "setup_type": "OI_WALL_REJECTION",
        "direction": "BEARISH",
        "entry_price": 22552.10,
        "stop_loss": 22577.10,
        "target_1": 22527.10,
        "target_2": 22472.10,
        "state": "OPEN",
        "entry_timestamp": "2026-10-23T04:00:00+00:00",
        "market_context": {
            "option_trailing": opt_ctx.to_dict(),
        },
    }
    pm.active_trades = [trade]

    rendered_embeds = []

    async def mock_send(t, spot, update_type):
        from alerts import trade_update_action_text, _option_lifecycle_estimate
        action = trade_update_action_text(update_type, t)
        est = _option_lifecycle_estimate(t, spot, update_type)
        rendered_embeds.append((update_type, action, est))

    with patch("position_manager.send_trade_update", side_effect=mock_send), \
         patch.object(pm.analytics, "apply_option_lifecycle_transition"):

        # Spot rises to 22580.00 (crosses BEARISH SL 22577.10)
        events = await pm.update_trades(spot_price=22580.00, candle_high=22580.00, candle_low=22550.00)
        assert ("trade-3935", "SL_HIT") in events
        assert trade["state"] == "CLOSED"
        opt = trade["market_context"]["option_trailing"]
        assert opt["phase"] == "CLOSED"
        assert opt["highest_milestone"] == 0
        assert opt["booked_fraction"] == 0.0
        assert opt["remaining_fraction"] == 0.0

        # Check rendered Card F (Original Index SL exit)
        assert any(
            e[0] == "SL_HIT" and "Index Stop Loss Hit before T1. 100% Closed (Score: 0)" in e[1]
            for e in rendered_embeds
        )
