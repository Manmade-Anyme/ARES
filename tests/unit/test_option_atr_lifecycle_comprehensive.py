"""Comprehensive unit test suite for Option ATR trailing, milestone accounting, and Discord alerts.
Validates the full test matrix specified in TASK-20261023.
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from models import AresSignal, SetupType, Direction, OptionTrailingContract, OptionTrailingContext
from position_manager import PositionManager
from options_candles import OptionCandle
from alerts import (
    trade_update_action_text,
    send_trade_update,
    send_monitoring_alert,
    send_persistence_alert,
)
from storage import AnalyticsLogger

IST = timezone(timedelta(hours=5, minutes=30))


def _make_signal(
    signal_id="sig-test-1",
    display_id="1950",
    direction=Direction.BEARISH,
    spot=22552.10,
    sl=22577.10,
    t1=22527.10,
    t2=22472.10,
    strike=22550,
    option_type="PE",
    opt_premium=139.25,
    opt_sl=123.65,
):
    sig = AresSignal(
        setup_type=SetupType.OI_WALL_REJECTION,
        direction=direction,
        trigger_price=spot,
        entry_zone=(spot - 5, spot + 5),
        stop_loss=sl,
        target_1=t1,
        target_2=t2,
        confidence="HIGH",
        reasons=["OI rejection at wall"],
        timestamp=datetime(2026, 10, 23, 9, 30, tzinfo=IST),
        strike_to_trade=strike,
        option_type=option_type,
        display_id=display_id,
    )
    sig.id = signal_id
    sig.option_premium = opt_premium
    sig.option_sl = opt_sl
    sig.suggested_lots = 2
    return sig


def _mock_trade_from_signal(signal: AresSignal, trade_id="trade-1950", spot=22552.10):
    contract = OptionTrailingContract(
        security_id="51333",
        exchange_segment="NSE_FNO",
        expiry="2026-10-29",
        strike=signal.strike_to_trade,
        option_type=signal.option_type,
    )
    opt_ctx = OptionTrailingContext(
        policy_version="option_atr_v1",
        revision=1,
        contract=contract,
        phase="PRE_T1",
        highest_milestone=0,
        booked_fraction=0.0,
        remaining_fraction=1.0,
        original_option_sl=signal.option_sl,
        original_option_entry=signal.option_premium,
        index_guard_enabled=True,
        index_guard_stop=spot,
    )
    return {
        "id": trade_id,
        "signal_id": str(signal.id),
        "display_id": signal.display_id,
        "setup_type": signal.setup_type.value,
        "direction": signal.direction.value,
        "entry_price": float(spot),
        "stop_loss": float(signal.stop_loss),
        "target_1": float(signal.target_1),
        "target_2": float(signal.target_2),
        "state": "OPEN",
        "entry_timestamp": "2026-10-23T04:00:00+00:00",
        "market_context": {
            "option_trailing": opt_ctx.to_dict(),
        },
    }


def _make_pm():
    pm = PositionManager()
    pm.is_initialized = True
    return pm


# ==============================================================================
# 1. Milestone Lifecycle & Dual Protection Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_pre_t1_index_sl_hit_closes_100_percent_score_0():
    """Index SL hit before T1 closes 100% with score 0; option ATR remains inactive."""
    pm = _make_pm()
    sig = _make_signal()
    trade = _mock_trade_from_signal(sig)
    pm.active_trades = [trade]

    # Index SL for BEARISH trade is 22577.10. Spot touches 22580.00
    with patch.object(pm.analytics, "apply_option_lifecycle_transition") as mock_rpc, \
         patch("position_manager.send_trade_update", new=AsyncMock()):
        events = await pm.update_trades(spot_price=22580.00)
        assert ("trade-1950", "SL_HIT") in events
        assert trade["state"] == "CLOSED"
        opt = trade["market_context"]["option_trailing"]
        assert opt["phase"] == "CLOSED"
        assert opt["highest_milestone"] == 0
        assert opt["booked_fraction"] == 0.0
        assert opt["remaining_fraction"] == 0.0


@pytest.mark.asyncio
async def test_index_t1_reached_books_50_percent_score_1_moves_sl_to_entry():
    """T1 reached books 50% (score 1), moves index SL to entry, activates POST_T1 phase."""
    pm = _make_pm()
    sig = _make_signal()
    trade = _mock_trade_from_signal(sig)
    pm.active_trades = [trade]

    # BEARISH target_1 is 22527.10. Spot touches 22525.00
    with patch.object(pm.analytics, "apply_option_lifecycle_transition") as mock_rpc, \
         patch("position_manager.send_trade_update", new=AsyncMock()):
        events = await pm.update_trades(spot_price=22525.00)
        assert ("trade-1950", "T1_HIT") in events
        assert trade["state"] == "T1_HIT"
        assert trade["stop_loss"] == trade["entry_price"]  # Break-even index guard
        opt = trade["market_context"]["option_trailing"]
        assert opt["phase"] == "POST_T1"
        assert opt["highest_milestone"] == 1
        assert opt["booked_fraction"] == 0.5
        assert opt["remaining_fraction"] == 0.5
        assert opt["index_guard_enabled"] is True


@pytest.mark.asyncio
async def test_dual_protection_index_entry_exit_between_t1_and_t2():
    """Between T1 and T2, index entry break-even touch closes remaining 50% with score 1 retained."""
    pm = _make_pm()
    sig = _make_signal()
    trade = _mock_trade_from_signal(sig)
    pm.active_trades = [trade]

    # Reach T1
    with patch.object(pm.analytics, "apply_option_lifecycle_transition"), \
         patch("position_manager.send_trade_update", new=AsyncMock()):
        await pm.update_trades(spot_price=22525.00)

    # Spot rallies back to entry (22552.10)
    with patch.object(pm.analytics, "apply_option_lifecycle_transition"), \
         patch("position_manager.send_trade_update", new=AsyncMock()):
        events = await pm.update_trades(spot_price=22553.00)
        assert ("trade-1950", "STOPPED_OUT_AT_BE") in events
        assert trade["state"] == "CLOSED"
        opt = trade["market_context"]["option_trailing"]
        assert opt["phase"] == "CLOSED"
        assert opt["highest_milestone"] == 1
        assert opt["booked_fraction"] == 1.0
        assert opt["remaining_fraction"] == 0.0


@pytest.mark.asyncio
async def test_index_t2_reached_books_cumulative_80_percent_score_2_disables_index_sl():
    """T2 reached books additional 30% (80% cumulative, 20% runner), score 2, disables index SL."""
    pm = _make_pm()
    sig = _make_signal()
    trade = _mock_trade_from_signal(sig)
    pm.active_trades = [trade]

    # Reach T1 first
    with patch.object(pm.analytics, "apply_option_lifecycle_transition"), \
         patch("position_manager.send_trade_update", new=AsyncMock()):
        await pm.update_trades(spot_price=22525.00)

    # BEARISH target_2 is 22472.10. Spot touches 22470.00
    with patch.object(pm.analytics, "apply_option_lifecycle_transition"), \
         patch("position_manager.send_trade_update", new=AsyncMock()):
        events = await pm.update_trades(spot_price=22470.00)
        assert ("trade-1950", "T2_HIT") in events
        assert trade["state"] == "T2_REACHED"
        opt = trade["market_context"]["option_trailing"]
        assert opt["phase"] == "POST_T2"
        assert opt["highest_milestone"] == 2
        assert opt["booked_fraction"] == 0.8
        assert opt["remaining_fraction"] == 0.2
        assert opt["index_guard_enabled"] is False  # Index SL disabled!
        # Fallback floor active if ATR has not exceeded original option SL
        assert opt["locked_option_sl"] == 123.65
        assert opt["protection_mode"] == "ORIGINAL_OPTION_SL_FLOOR"


@pytest.mark.asyncio
async def test_same_candle_t1_and_t2_hit_applies_both_in_order():
    """If one index candle touches both T1 and T2, applies T1 then T2 exactly once."""
    pm = _make_pm()
    sig = _make_signal()
    trade = _mock_trade_from_signal(sig)
    pm.active_trades = [trade]

    # Candle touches 22470 (which is beyond both T1 22527 and T2 22472)
    with patch.object(pm.analytics, "apply_option_lifecycle_transition"), \
         patch("position_manager.send_trade_update", new=AsyncMock()):
        events = await pm.update_trades(spot_price=22470.00, candle_low=22470.00, candle_high=22550.00)
        assert events == [("trade-1950", "T1_HIT"), ("trade-1950", "T2_HIT")]
        assert trade["state"] == "T2_REACHED"
        opt = trade["market_context"]["option_trailing"]
        assert opt["highest_milestone"] == 2
        assert opt["booked_fraction"] == 0.8
        assert opt["remaining_fraction"] == 0.2


# ==============================================================================
# 2. Option Candle Evaluation & Strict Close Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_option_atr_exit_requires_completed_close_strictly_below_stop():
    """An option stop exit requires candle close strictly below stop; wicks and equal close do not trigger."""
    pm = _make_pm()
    sig = _make_signal()
    trade = _mock_trade_from_signal(sig)
    trade["state"] = "T1_HIT"
    opt = trade["market_context"]["option_trailing"]
    opt["phase"] = "POST_T1"
    opt["highest_milestone"] = 1
    opt["t1_at"] = "2026-10-23T09:30:00+05:30"
    opt["locked_option_sl"] = 130.00  # Active stop is 130.00
    opt["protection_mode"] = "ATR"
    pm.active_trades = [trade]

    base = datetime(2026, 10, 23, 9, 35, tzinfo=IST)

    # 1. Bar wicks down to 125.00 but closes at 131.00 -> NO EXIT
    candle_wick = OptionCandle(
        start_time=base,
        end_time=base + timedelta(minutes=5),
        open=132.0,
        high=135.0,
        low=125.0,  # Wicked below 130
        close=131.0,  # Closed above 130
        volume=1000.0,
        security_id="51333",
        complete=True,
    )

    with patch.object(pm.analytics, "apply_option_lifecycle_transition"), \
         patch("position_manager.send_trade_update", new=AsyncMock()):
        events = await pm.update_option_atr_trailing([candle_wick])
        assert trade["state"] == "T1_HIT"  # Still open!
        assert len(events) == 0

    # 2. Bar closes exactly at stop 130.00 -> NO EXIT (strictly below required)
    candle_equal = OptionCandle(
        start_time=base + timedelta(minutes=5),
        end_time=base + timedelta(minutes=10),
        open=131.0,
        high=133.0,
        low=129.0,
        close=130.0,  # Equal close
        volume=1000.0,
        security_id="51333",
        complete=True,
    )
    with patch.object(pm.analytics, "apply_option_lifecycle_transition"), \
         patch("position_manager.send_trade_update", new=AsyncMock()):
        events = await pm.update_option_atr_trailing([candle_equal])
        assert trade["state"] == "T1_HIT"

    # 3. Bar closes strictly below at 129.95 -> EXIT TRIGGERED
    candle_exit = OptionCandle(
        start_time=base + timedelta(minutes=10),
        end_time=base + timedelta(minutes=15),
        open=130.0,
        high=131.0,
        low=128.0,
        close=129.95,  # Strictly below 130.00
        volume=1000.0,
        security_id="51333",
        complete=True,
    )
    with patch.object(pm.analytics, "apply_option_lifecycle_transition"), \
         patch("position_manager.send_trade_update", new=AsyncMock()):
        events = await pm.update_option_atr_trailing([candle_exit])
        assert ("trade-1950", "ATR_SL_HIT") in events
        assert trade["state"] == "CLOSED"
        assert opt["phase"] == "CLOSED"
        assert opt["remaining_fraction"] == 0.0


@pytest.mark.asyncio
async def test_post_t2_runner_exit_retains_score_2():
    """Post-T2 runner exit closes final 20% and retains score 2."""
    pm = _make_pm()
    sig = _make_signal()
    trade = _mock_trade_from_signal(sig)
    trade["state"] = "T2_REACHED"
    opt = trade["market_context"]["option_trailing"]
    opt["phase"] = "POST_T2"
    opt["highest_milestone"] = 2
    opt["booked_fraction"] = 0.8
    opt["remaining_fraction"] = 0.2
    opt["t1_at"] = "2026-10-23T09:30:00+05:30"
    opt["t2_at"] = "2026-10-23T09:45:00+05:30"
    opt["locked_option_sl"] = 145.00
    opt["protection_mode"] = "ATR"
    pm.active_trades = [trade]

    base = datetime(2026, 10, 23, 9, 50, tzinfo=IST)
    candle_runner_exit = OptionCandle(
        start_time=base,
        end_time=base + timedelta(minutes=5),
        open=146.0,
        high=147.0,
        low=140.0,
        close=143.50,  # Closes below 145.00
        volume=1000.0,
        security_id="51333",
        complete=True,
    )

    with patch.object(pm.analytics, "apply_option_lifecycle_transition"), \
         patch("position_manager.send_trade_update", new=AsyncMock()):
        events = await pm.update_option_atr_trailing([candle_runner_exit])
        assert ("trade-1950", "ATR_SL_HIT") in events
        assert trade["state"] == "CLOSED"
        assert opt["highest_milestone"] == 2  # Score 2 retained!
        assert opt["booked_fraction"] == 1.0
        assert opt["remaining_fraction"] == 0.0


# ==============================================================================
# 3. Transactional Persistence & Deduplication Tests
# ==============================================================================

def test_storage_apply_option_lifecycle_transition_fallback():
    """Tests client-side fallback of apply_option_lifecycle_transition with revision & score retention."""
    logger = AnalyticsLogger()
    mock_supabase = MagicMock()
    logger.supabase = mock_supabase

    state = {
        "market_context": {
            "option_trailing": {
                "policy_version": "option_atr_v1",
                "revision": 1,
                "phase": "PRE_T1",
                "highest_milestone": 0,
                "remaining_fraction": 1.0,
                "alert_events": {},
            }
        },
        "score": None,
        "result_state": "OPEN",
    }
    
    mock_supabase.table().select().eq().execute.side_effect = lambda: MagicMock(data=[state])
    def do_update(payload):
        state.update(payload)
        return MagicMock(eq=lambda *a, **k: MagicMock(execute=lambda: MagicMock(data=[state])))
    mock_supabase.table().update.side_effect = do_update
    mock_supabase.rpc.side_effect = Exception("RPC not in test mock")

    # Transition to T1
    payload = {
        "active_state": "T1_HIT",
        "stop_loss": 22552.10,
        "score": 1,
        "update_type": "T1_HIT",
        "option_trailing": {
            "phase": "POST_T1",
            "highest_milestone": 1,
            "booked_fraction": 0.5,
            "remaining_fraction": 0.5,
        }
    }
    event_id = "test-trade:POST_T1:1:T1_HIT"
    res = logger.apply_option_lifecycle_transition("test-trade", 1, event_id, payload)
    assert res["success"] is True
    assert res["revision"] == 2
    assert res["result_state"] == "OPEN"
    assert res["score"] == 1

    # Idempotent re-submission should skip
    res_dup = logger.apply_option_lifecycle_transition("test-trade", 2, event_id, payload)
    assert res_dup["idempotent_skip"] is True


def test_startup_reconciliation_keeps_active_runner_open():
    """Startup reconciliation does not treat T2_REACHED or open milestone score as terminal."""
    pm = PositionManager()
    mock_supabase = MagicMock()
    pm.supabase = mock_supabase

    active_rows = [{
        "id": "trade-runner-open",
        "state": "T2_REACHED",
        "stop_loss": 22552.10,
        "entry_price": 22552.10,
        "market_context": {
            "option_trailing": {
                "policy_version": "option_atr_v1",
                "phase": "POST_T2",
                "highest_milestone": 2,
                "remaining_fraction": 0.2,
            }
        }
    }]
    analytics_rows = [{
        "id": "trade-runner-open",
        "result_state": "OPEN",  # OPEN in analytics!
        "score": 2,
        "exit_price": None,
        "exit_timestamp": None,
        "market_context": active_rows[0]["market_context"],
    }]

    mock_supabase.table("active_trades").select("*").execute.return_value = MagicMock(data=active_rows)
    mock_supabase.table("trade_analytics").select().in_().execute.return_value = MagicMock(data=analytics_rows)

    pm._initialize_db()
    assert len(pm.active_trades) == 1
    assert pm.active_trades[0]["id"] == "trade-runner-open"
    assert pm.active_trades[0]["state"] == "T2_REACHED"


# ==============================================================================
# 4. Discord Message Catalog Tests (Cards A - L)
# ==============================================================================

def test_trade_update_action_texts():
    """Verifies descriptive strings for all Cards in catalog."""
    trade_opt = {
        "state": "T1_HIT",
        "stop_loss": 22552.10,
        "entry_price": 22552.10,
        "market_context": {
            "option_trailing": {
                "policy_version": "option_atr_v1",
                "phase": "POST_T1",
                "highest_milestone": 1,
            }
        }
    }
    assert "Target 1 Reached! 50% model-booked" in trade_update_action_text("T1_HIT", trade_opt)
    assert "Target 2 Reached! Cumulative 80% model-booked" in trade_update_action_text("T2_HIT", trade_opt)
    assert "Option ATR Protection Activated" in trade_update_action_text("ATR_ACTIVE", trade_opt)
    assert "Option ATR Stop Loss Tightened" in trade_update_action_text("OPTION_SL_RATCHETED", trade_opt)
    assert "Remaining 50% Closed (Score: 1 retained)" in trade_update_action_text("STOPPED_OUT_AT_BE", trade_opt)
    assert "Remaining 50% Closed (Score: 1 retained)" in trade_update_action_text("ATR_SL_HIT", trade_opt)

    trade_runner = {
        **trade_opt,
        "state": "T2_REACHED",
        "market_context": {
            "option_trailing": {
                "policy_version": "option_atr_v1",
                "phase": "POST_T2",
                "highest_milestone": 2,
            }
        }
    }
    assert "Final 20% Runner Closed (Score: 2 retained)" in trade_update_action_text("ATR_SL_HIT", trade_runner)
    assert "Final 20% Runner Closed (Score: 2 retained)" in trade_update_action_text("OPTION_SL_FLOOR_HIT", trade_runner)
    assert "Index Stop Loss Hit before T1" in trade_update_action_text("SL_HIT", {"state": "OPEN", "stop_loss": 22577, "entry_price": 22552, "market_context": {"option_trailing": {"policy_version": "option_atr_v1"}}})


@pytest.mark.asyncio
async def test_discord_alert_deduplication():
    """Verifies duplicate alert events are suppressed via alert_events state."""
    trade = {
        "id": "trade-dedup",
        "display_id": "1950",
        "setup_type": "OI_WALL_REJECTION",
        "direction": "BEARISH",
        "entry_price": 22552.10,
        "stop_loss": 22552.10,
        "state": "T1_HIT",
        "market_context": {
            "option_trailing": {
                "policy_version": "option_atr_v1",
                "revision": 2,
                "phase": "POST_T1",
                "alert_events": {},
            }
        }
    }

    with patch("alerts.settings") as mock_settings, \
         patch("httpx.AsyncClient") as mock_http_client:
        mock_settings.discord_webhook_url = "http://mock-webhook"
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_http_client.return_value.__aenter__.return_value = mock_client

        # First alert dispatch: posts to webhook
        await send_trade_update(trade, 22525.00, "T1_HIT")
        assert mock_client.post.call_count == 1
        event_key = "trade-dedup:POST_T1:2:T1_HIT"
        assert trade["market_context"]["option_trailing"]["alert_events"][event_key]["status"] == "DELIVERED"

        # Duplicate alert dispatch: skipped!
        mock_client.reset_mock()
        await send_trade_update(trade, 22525.00, "T1_HIT")
        assert mock_client.post.call_count == 0
