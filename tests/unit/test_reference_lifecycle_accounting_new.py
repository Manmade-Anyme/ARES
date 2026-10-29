from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import pytest

from tests.unit.test_option_atr_lifecycle_comprehensive import _make_pm, _make_signal, _mock_trade_from_signal

IST = timezone(timedelta(hours=5, minutes=30))


def reference_trade():
    trade = _mock_trade_from_signal(_make_signal())
    trade["market_context"]["option_trailing"]["accounting_formula_version"] = "one_lot_reference_v1"
    return trade


@pytest.mark.parametrize("direction", ["BULLISH", "BEARISH"])
def test_weighted_reference_totals_and_missing_evidence(direction):
    pm = _make_pm()
    trade = reference_trade()
    trade.update(direction=direction, entry_price=100)
    opt = trade["market_context"]["option_trailing"]
    opt.update(original_option_entry=50, highest_milestone=2, phase="CLOSED")
    sign = 1 if direction == "BULLISH" else -1
    opt["accounting"] = {"legs": {
        "T1": {"spot_price": 100 + sign * 10, "option_price": 60},
        "T2": {"spot_price": 100 + sign * 20, "option_price": 70},
        "EXIT": {"spot_price": 100 + sign * 15, "option_price": 65},
    }}
    totals = pm._reference_totals(trade)
    assert totals["spot_total_points"] == 14
    assert totals["option_total_points"] == 14
    del totals["legs"]["T2"]["option_price"]
    assert pm._reference_totals(trade)["option_total_points"] is None
    assert totals["spot_total_points"] == 14


@pytest.mark.asyncio
async def test_same_candle_t2_and_guard_remains_unresolved():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22525, event_time=datetime(2026, 10, 23, 9, 31, tzinfo=IST))
        events = await pm.update_trades(22500, candle_high=22553, candle_low=22470,
            candle_timestamp=datetime(2026, 10, 23, 9, 32, tzinfo=IST),
            event_time=datetime(2026, 10, 23, 9, 33, tzinfo=IST))
    opt = trade["market_context"]["option_trailing"]
    assert events == []
    assert trade["state"] == "T1_HIT"
    assert opt["highest_milestone"] == 1
    assert opt["ambiguous_index_touch"]["status"] == "ORDER_UNKNOWN"


@pytest.mark.asyncio
async def test_t2_wick_in_the_minute_t1_was_observed_is_not_discarded():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    minute = datetime(2026, 10, 23, 9, 32, tzinfo=IST)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22525, event_time=minute + timedelta(seconds=20))
        events = await pm.update_trades(22500, candle_high=22540, candle_low=22470,
            candle_timestamp=minute)
    opt = trade["market_context"]["option_trailing"]
    assert events == [(trade["id"], "T2_HIT")]
    assert trade["state"] == "T2_REACHED"
    assert opt["highest_milestone"] == 2
    assert opt["accounting"]["legs"]["T2"]["source_minute"] == minute.isoformat()


@pytest.mark.asyncio
async def test_provider_certified_tick_history_resolves_ambiguity_and_persists_clear():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    minute = datetime(2026, 10, 23, 9, 32, tzinfo=IST)
    ticks = [{"observed_at": (minute + timedelta(seconds=second)).isoformat(),
              "price": price, "source": "dhan_websocket_tick"}
             for second, price in [(5, 22560), (20, 22525), (35, 22470), (45, 22553)]]
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22525, event_time=minute + timedelta(seconds=20))
        await pm.update_trades(22500, candle_high=22553, candle_low=22470,
            candle_timestamp=minute)
        feed = SimpleNamespace(get_complete_minute_observations=lambda _: (ticks, True))
        assert await pm.resolve_reference_ambiguities_from_feed(feed) == 1
    opt = trade["market_context"]["option_trailing"]
    assert trade["state"] == "T2_REACHED"
    assert opt["highest_milestone"] == 2
    assert opt["ambiguous_index_touch"] is None
    assert pm.analytics.rows[-1]["option_trailing"]["ambiguous_index_touch"] is None
    from models import OptionTrailingContext
    restored = OptionTrailingContext.from_dict(pm.analytics.rows[-1]["option_trailing"])
    assert restored.ambiguous_index_touch is None


@pytest.mark.asyncio
async def test_incomplete_tick_feed_cannot_resolve_ambiguity():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    minute = datetime(2026, 10, 23, 9, 32, tzinfo=IST)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22525, event_time=minute + timedelta(seconds=20))
        await pm.update_trades(22500, candle_high=22553, candle_low=22470,
            candle_timestamp=minute)
        feed = SimpleNamespace(get_complete_minute_observations=lambda _: ([], False))
        assert await pm.resolve_reference_ambiguities_from_feed(feed) == 0
    assert trade["state"] == "T1_HIT"
    assert trade["market_context"]["option_trailing"]["ambiguous_index_touch"]["status"] == "ORDER_UNKNOWN"


@pytest.mark.asyncio
async def test_buffered_pre_entry_tick_is_skipped_only_for_trade_not_yet_entered():
    pm = _make_pm()
    old_trade = {
        "id": "old-trade", "state": "OPEN", "direction": "BULLISH",
        "entry_price": 100, "stop_loss": 90, "target_1": 110, "target_2": 120,
        "entry_timestamp": "2026-10-23T10:00:00+00:00",
    }
    new_trade = {**old_trade, "id": "new-trade",
                 "entry_timestamp": "2026-10-23T10:05:00+00:00"}
    pm.active_trades = [old_trade, new_trade]
    pm._record_trade_update = AsyncMock()

    events = await pm.update_trades(
        89, event_time=datetime(2026, 10, 23, 10, 6, tzinfo=timezone.utc),
        market_event_time=datetime(2026, 10, 23, 10, 4, tzinfo=timezone.utc),
    )

    assert events == [("old-trade", "SL_HIT")]
    assert old_trade["state"] == "CLOSED"
    assert new_trade["state"] == "OPEN"


@pytest.mark.asyncio
async def test_signal_reference_ignores_ticks_before_registration_time():
    pm = _make_pm()
    trade = {
        "id": "late-registration", "state": "OPEN", "direction": "BULLISH",
        "entry_price": 100, "stop_loss": 90, "target_1": 110, "target_2": 120,
        "entry_timestamp": "2026-10-23T10:00:00+00:00",
        "created_at": "2026-10-23T10:05:00+00:00",
        "market_context": {"tracking_mode": "SIGNAL_REFERENCE"},
    }
    pm.active_trades = [trade]
    pm._record_trade_update = AsyncMock()

    events = await pm.update_trades(
        89, event_time=datetime(2026, 10, 23, 10, 6, tzinfo=timezone.utc),
        market_event_time=datetime(2026, 10, 23, 10, 4, 59, tzinfo=timezone.utc),
    )

    assert events == []
    assert trade["state"] == "OPEN"


@pytest.mark.asyncio
async def test_same_second_exchange_tick_after_entry_is_not_rejected_as_stale():
    pm = _make_pm()
    trade = {
        "id": "same-second-trade", "state": "OPEN", "direction": "BULLISH",
        "entry_price": 100, "stop_loss": 90, "target_1": 110, "target_2": 120,
        "entry_timestamp": "2026-10-23T10:05:00.500000+00:00",
    }
    pm.active_trades = [trade]
    pm._record_trade_update = AsyncMock()

    events = await pm.update_trades(
        111, event_time=datetime(2026, 10, 23, 10, 5, 0, 600000, tzinfo=timezone.utc),
        market_event_time=datetime(2026, 10, 23, 10, 5, tzinfo=timezone.utc),
    )

    assert events == [("same-second-trade", "T1_HIT")]


@pytest.mark.asyncio
async def test_buffered_tick_before_option_activation_is_ignored_even_after_late_receipt():
    pm = _make_pm()
    trade = {
        "id": "late-activation-trade", "state": "OPEN", "direction": "BULLISH",
        "entry_price": 100, "stop_loss": 90, "target_1": 110, "target_2": 120,
        "entry_timestamp": "2026-10-23T10:00:00+00:00",
        "market_context": {"option_trailing": {
            "policy_version": "option_atr_v1",
            "activated_at": "2026-10-23T10:05:00+00:00",
        }},
    }
    pm.active_trades = [trade]
    pm._record_trade_update = AsyncMock()

    events = await pm.update_trades(
        89, event_time=datetime(2026, 10, 23, 10, 6, tzinfo=timezone.utc),
        market_event_time=datetime(2026, 10, 23, 10, 4, 59, tzinfo=timezone.utc),
    )

    assert events == []
    assert trade["state"] == "OPEN"


@pytest.mark.asyncio
async def test_same_buffered_batch_can_record_t2_after_tick_driven_t1():
    pm = _make_pm()
    trade = {
        "id": "buffered-runner", "state": "OPEN", "direction": "BULLISH",
        "entry_price": 100, "stop_loss": 90, "target_1": 110, "target_2": 120,
        "entry_timestamp": "2026-10-23T09:00:00+00:00",
        "market_context": {"legacy_t2_tracking_approved": True},
    }
    pm.active_trades = [trade]
    pm._record_trade_update = AsyncMock()
    pm._queue_trade_alert = lambda *args: None
    t1_at = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)

    t1_events = await pm.update_trades(110, event_time=t1_at)
    t2_events = await pm.update_trades(120, event_time=t1_at + timedelta(seconds=1))

    assert t1_events == [("buffered-runner", "T1_HIT")]
    assert t2_events == [("buffered-runner", "T2_TOUCHED")]
    assert trade["market_context"]["target_milestones"]["t2"]["touched_at"] == (t1_at + timedelta(seconds=1)).isoformat()


@pytest.mark.asyncio
async def test_new_spot_atr_registration_enrolls_t2_milestone_tracking(monkeypatch):
    from config import settings
    from position_manager import PositionManager
    from tests.unit.test_manm176_review_persistence import MemoryDatabase, make_signal

    monkeypatch.setattr(settings, "option_atr_enabled", False)
    monkeypatch.setattr(settings, "option_atr_approved_version", "")
    monkeypatch.setattr(settings, "supabase_service_role_key", "")
    monkeypatch.setattr(settings, "signal_schema_mode", "bridge")
    database = MemoryDatabase()
    signal = make_signal()
    signal.market_context = {"tracking_mode": "SIGNAL_REFERENCE"}
    with patch("position_manager.create_client", return_value=database), \
            patch("position_manager.AnalyticsLogger"):
        pm = PositionManager()
        _, status = await pm.add_trade(signal, 24000)

    trade = pm.active_trades[0]
    assert status == "BOUND"
    assert trade["market_context"]["legacy_t2_tracking_approved"] is True
    pm._record_trade_update = AsyncMock()
    pm._queue_trade_write = lambda *args: None
    pm._queue_trade_alert = lambda *args: None
    t1_at = datetime.fromisoformat(trade["created_at"]) + timedelta(seconds=1)
    assert await pm.update_trades(24025, event_time=t1_at) == [(trade["id"], "T1_HIT")]
    assert await pm.update_trades(24040, event_time=t1_at + timedelta(seconds=1)) == [
        (trade["id"], "T2_TOUCHED")
    ]
    assert "t2" in trade["market_context"]["target_milestones"]


def test_timestamped_spot_tick_is_used_for_atr_exit_accounting():
    pm = _make_pm()
    trade = reference_trade()
    close_at = datetime(2026, 10, 23, 10, 35, tzinfo=IST)
    pm.record_reference_spot_quote(22501.25, close_at - timedelta(seconds=2), "dhan_websocket_tick")
    opt = trade["market_context"]["option_trailing"]
    opt["accounting"]["legs"]["EXIT"] = {
        "event_at": close_at.isoformat(), "spot_price": None,
        "option_price": 145, "option_source": "completed_5m_close",
    }
    pm._enrich_reference_trade(trade)
    assert opt["accounting"]["legs"]["EXIT"]["spot_price"] == 22501.25
    assert opt["accounting"]["legs"]["EXIT"]["spot_source"] == "dhan_websocket_tick"


@pytest.mark.asyncio
@pytest.mark.parametrize("first,expected", [(22470, "T2_REACHED"), (22553, "CLOSED")])
async def test_ambiguity_resolves_only_from_complete_ordered_ticks(first, expected):
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    base = datetime(2026, 10, 23, 9, 32, tzinfo=IST)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22525, event_time=base - timedelta(minutes=1))
        await pm.update_trades(22500, candle_high=22553, candle_low=22470,
            candle_timestamp=base, event_time=base + timedelta(minutes=1))
        other = 22553 if first == 22470 else 22470
        observations = [{"observed_at": (base + timedelta(seconds=i)).isoformat(),
                         "price": price, "source": "complete_tick_history"}
                        for i, price in [(5, first), (20, other)]]
        assert not await pm.resolve_reference_ambiguity(trade["id"], observations)
        assert trade["state"] == "T1_HIT"
        assert await pm.resolve_reference_ambiguity(trade["id"], observations, complete=True)
    assert trade["state"] == expected
    assert trade["market_context"]["option_trailing"]["highest_milestone"] == (2 if expected == "T2_REACHED" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("first,expected", [(22520, "CLOSED"), (22553, "T1_HIT")])
async def test_t1_and_entry_same_candle_requires_ordered_tick_evidence(first, expected):
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    start = datetime(2026, 10, 23, 9, 32, tzinfo=IST)
    other = 22553 if first == 22520 else 22520
    ticks = [{"observed_at": (start + timedelta(seconds=sec)).isoformat(),
              "price": price, "source": "complete_tick_history"}
             for sec, price in [(5, first), (30, other)]]
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22530, candle_high=22553, candle_low=22520,
            candle_timestamp=start, event_time=start + timedelta(minutes=1))
        opt = trade["market_context"]["option_trailing"]
        assert trade["state"] == "T1_HIT"
        assert opt["highest_milestone"] == 1
        assert opt["ambiguous_index_touch"]["kind"] == "T1_ENTRY"
        assert not await pm.resolve_reference_ambiguity(trade["id"], ticks)
        assert await pm.resolve_reference_ambiguity(trade["id"], ticks, complete=True)
    assert trade["state"] == expected
    assert opt["highest_milestone"] == 1
    assert opt.get("ambiguous_index_touch") is None


@pytest.mark.asyncio
async def test_t1_entry_ambiguity_replays_later_t2():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    start = datetime(2026, 10, 23, 9, 32, tzinfo=IST)
    ticks = [{"observed_at": (start + timedelta(seconds=sec)).isoformat(),
              "price": price, "source": "complete_tick_history"}
             for sec, price in [(5, 22553), (30, 22520)]]
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22530, candle_high=22553, candle_low=22520,
            candle_timestamp=start, event_time=start + timedelta(minutes=1))
        await pm.update_trades(22470, event_time=start + timedelta(minutes=2))
        opt = trade["market_context"]["option_trailing"]
        assert opt["ambiguous_index_touch"]["subsequent_index_observations"]
        assert await pm.resolve_reference_ambiguity(trade["id"], ticks, complete=True)
    assert trade["state"] == "T2_REACHED"
    assert opt["highest_milestone"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("later_price,atr_before,expected,score", [
    (22470, True, "ATR_SL_HIT", 1),
    (22470, False, "ATR_SL_HIT", 2),
    (22553, False, "STOPPED_OUT_AT_BE", 1),
])
async def test_t1_entry_recovery_orders_later_touch_and_atr(later_price, atr_before, expected, score):
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    start = datetime(2026, 10, 23, 9, 32, tzinfo=IST)
    ticks = [{"observed_at": (start + timedelta(seconds=sec)).isoformat(),
              "price": price, "source": "complete_tick_history"}
             for sec, price in [(5, 22553), (30, 22520)]]
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22530, candle_high=22553, candle_low=22520,
            candle_timestamp=start, event_time=start + timedelta(minutes=1))
        await pm.update_trades(later_price, event_time=start + timedelta(minutes=3))
        opt = trade["market_context"]["option_trailing"]
        opt["ambiguous_index_touch"]["protective_option_exit"] = {
            "event_at": (start + timedelta(minutes=2 if atr_before else 4)).isoformat(),
            "option_close": 145, "option_stop": 150, "reason": "ATR_SL_HIT"}
        assert await pm.resolve_reference_ambiguity(trade["id"], ticks, complete=True)
    assert trade["state"] == "CLOSED"
    assert opt["highest_milestone"] == score
    assert opt["exit"]["reason"] == expected


@pytest.mark.asyncio
async def test_t1_entry_recovery_preserves_equal_time_uncertainty():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    start = datetime(2026, 10, 23, 9, 32, tzinfo=IST)
    ticks = [{"observed_at": (start + timedelta(seconds=sec)).isoformat(),
              "price": price, "source": "complete_tick_history"}
             for sec, price in [(5, 22553), (30, 22520)]]
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22530, candle_high=22553, candle_low=22520,
            candle_timestamp=start, event_time=start + timedelta(minutes=1))
        await pm.update_trades(22470, event_time=start + timedelta(minutes=3))
        opt = trade["market_context"]["option_trailing"]
        opt["ambiguous_index_touch"]["protective_option_exit"] = {
            "event_at": (start + timedelta(minutes=3)).isoformat(),
            "option_close": 145, "option_stop": 150, "reason": "ATR_SL_HIT"}
        assert not await pm.resolve_reference_ambiguity(trade["id"], ticks, complete=True)
    assert trade["state"] == "T1_HIT"
    assert opt["highest_milestone"] == 1
    assert opt["ambiguous_index_touch"]["protective_option_exit"]["option_close"] == 145


@pytest.mark.asyncio
async def test_t1_entry_recovery_preserves_new_dual_touch_candle():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    start = datetime(2026, 10, 23, 9, 32, tzinfo=IST)
    ticks = [{"observed_at": (start + timedelta(seconds=sec)).isoformat(),
              "price": price, "source": "complete_tick_history"}
             for sec, price in [(5, 22553), (30, 22520)]]
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22530, candle_high=22553, candle_low=22520,
            candle_timestamp=start, event_time=start + timedelta(minutes=1))
        await pm.update_trades(22500, candle_high=22553, candle_low=22470,
            candle_timestamp=start + timedelta(minutes=1), event_time=start + timedelta(minutes=2))
        opt = trade["market_context"]["option_trailing"]
        assert not await pm.resolve_reference_ambiguity(trade["id"], ticks, complete=True)
    assert trade["state"] == "T1_HIT"
    assert opt["ambiguous_index_touch"]["kind"] == "T2_ENTRY"
    assert opt["ambiguous_index_touch"]["source_minute"] == (start + timedelta(minutes=1)).isoformat()


@pytest.mark.asyncio
async def test_legacy_option_trade_keeps_target_first_and_legacy_points():
    pm = _make_pm()
    trade = _mock_trade_from_signal(_make_signal())
    opt = trade["market_context"]["option_trailing"]
    opt.pop("accounting_formula_version", None)
    opt.pop("accounting", None)
    pm.active_trades = [trade]
    base = datetime(2026, 10, 23, 9, 32, tzinfo=IST)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22525, event_time=base - timedelta(minutes=1))
        await pm.update_trades(22500, candle_high=22553, candle_low=22470,
            candle_timestamp=base, event_time=base + timedelta(minutes=1))
    assert trade["state"] == "T2_REACHED"
    assert opt.get("ambiguous_index_touch") is None
    assert "accounting" not in opt
    assert "option_pnl_points" not in pm.analytics.rows[-1]
    trade["state"] = "CLOSED"
    opt.update(phase="CLOSED", remaining_fraction=0)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm._record_option_transition(trade, 22500, "ATR_SL_HIT", base.isoformat())
    assert pm.analytics.rows[-1]["pnl_points"] == pytest.approx(46.92)


@pytest.mark.asyncio
async def test_new_trade_includes_accounting_version_marker(monkeypatch):
    from config import settings
    from position_manager import PositionManager
    from tests.unit.test_manm176_review_persistence import MemoryDatabase, make_signal
    monkeypatch.setattr(settings, "option_atr_enabled", True)
    monkeypatch.setattr(settings, "option_atr_approved_version", "option_atr_v1")
    monkeypatch.setattr(settings, "supabase_service_role_key", "backend")
    signal = make_signal()
    signal.suggested_lots = signal.bracket.lots
    signal.option_premium = 99
    signal.option_sl = 95
    signal.market_context = {"tracking_mode": "SIGNAL_REFERENCE"}
    with patch("position_manager.create_client", return_value=MemoryDatabase()), patch("position_manager.AnalyticsLogger") as logger:
        logger.return_value.option_reference_accounting_ready.return_value = True
        pm = PositionManager()
        await pm.add_trade(signal, 24000)
    assert pm.active_trades[0]["market_context"]["option_trailing"]["accounting_formula_version"] == "one_lot_reference_v1"


@pytest.mark.asyncio
async def test_ambiguous_atr_exit_keeps_score_open_and_locked_stop(monkeypatch):
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    opt = trade["market_context"]["option_trailing"]
    opt.update(phase="POST_T1", highest_milestone=1, remaining_fraction=.5,
               locked_option_sl=150, protection_mode="ATR", t1_at="2026-10-23T09:30:00+05:30",
               ambiguous_index_touch={"source_minute":"2026-10-23T09:31:00+05:30", "status":"ORDER_UNKNOWN"})
    trade["state"] = "T1_HIT"
    bar = SimpleNamespace(start_time="2026-10-23T09:30:00+05:30",
                          end_time="2026-10-23T09:35:00+05:30", close=149, raw_line=200)
    monkeypatch.setattr(pm, "_prepare_option_history", lambda *args: {trade["id"]: [bar]})
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_option_atr_trailing({}, current_time=datetime(2026,10,23,9,35,tzinfo=IST), _check_monitoring=False)
        bar.end_time = "2026-10-23T09:40:00+05:30"
        bar.close = 148
        await pm.update_option_atr_trailing({}, current_time=datetime(2026,10,23,9,40,tzinfo=IST), _check_monitoring=False)
    assert trade["state"] == "T1_HIT"
    assert opt["highest_milestone"] == 1
    assert opt["locked_option_sl"] == 150
    assert opt["ambiguous_index_touch"]["protective_option_exit"]["option_close"] == 149


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", [False, True])
async def test_atr_exit_never_treats_unverified_mark_as_minute_close(monkeypatch, quote):
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    opt = trade["market_context"]["option_trailing"]
    end = datetime(2026,10,23,9,35,tzinfo=IST)
    opt.update(phase="POST_T1", highest_milestone=1, remaining_fraction=.5, locked_option_sl=150,
               protection_mode="ATR", t1_at="2026-10-23T09:30:00+05:30",
               last_index_mark={"price": 22500, "event_at": end.isoformat()})
    trade["state"] = "T1_HIT"
    if quote:
        pm.set_reference_price_evidence(spot_quotes=[{"price": 22501, "observed_at": end.isoformat(), "source": "spot_quote"}])
    bar = SimpleNamespace(start_time="2026-10-23T09:30:00+05:30", end_time=end.isoformat(), close=149, raw_line=145)
    monkeypatch.setattr(pm, "_prepare_option_history", lambda *args: {trade["id"]: [bar]})
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_option_atr_trailing({}, current_time=end, _check_monitoring=False)
    leg = opt["accounting"]["legs"]["EXIT"]
    assert leg["spot_price"] == (22501 if quote else None)
    assert leg["spot_source"] == ("spot_quote" if quote else None)


@pytest.mark.asyncio
async def test_stale_terminal_rebases_all_committed_leg_evidence(monkeypatch):
    from copy import deepcopy
    from tests.unit.test_option_conflict_recovery import ConflictLedger, post_t1
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    opt = post_t1(trade, stop=150)
    opt["accounting"] = {"legs": {"T1": {"spot_price": 22527.1, "option_price": None}}}
    remote = deepcopy(opt)
    remote.update(phase="POST_T2", highest_milestone=2, booked_fraction=.8, remaining_fraction=.2,
                  t2_at="2026-10-23T10:54:00+05:30")
    remote["accounting"]["legs"]["T1"]["option_price"] = 150
    remote["accounting"]["legs"]["T2"] = {"spot_price": 22472.1, "option_price": 160}
    pm.analytics = ConflictLedger(remote, state="T2_REACHED")
    trade["state"] = "CLOSED"
    opt.update(phase="CLOSED", remaining_fraction=0, exit={"reason":"ATR_SL_HIT", "event_at":"2026-10-23T10:55:00+05:30"})
    pm._capture_reference_leg(trade, "EXIT", None, "2026-10-23T10:55:00+05:30", option_close=155)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm._record_option_transition(trade, None, "ATR_SL_HIT", "2026-10-23T10:55:00+05:30")
    result = pm.analytics.rows[-1]
    assert result["score"] == 2
    assert result["option_trailing"]["accounting"]["legs"]["T1"]["option_price"] == 150
    assert result["option_trailing"]["accounting"]["legs"]["T2"]["option_price"] == 160
    assert result["option_pnl_points"] == pytest.approx(.5*(150-139.25)+.3*(160-139.25)+.2*(155-139.25))


@pytest.mark.asyncio
async def test_successful_commit_merges_server_evidence_into_pending_terminal():
    from copy import deepcopy
    from tests.unit.test_option_lifecycle_commit_boundary import Ledger

    class ServerMergedLedger(Ledger):
        def apply_option_lifecycle_transition(self, trade_id, revision, event_id, payload):
            result = super().apply_option_lifecycle_transition(trade_id, revision, event_id, payload)
            if len(self.rows) == 1:
                leg = result["option_trailing"]["accounting"]["legs"]["T1"]
                leg.update(option_price=150, option_source="completed_1m_close", approximate=True)
                self.rows[-1]["option_trailing"] = deepcopy(result["option_trailing"])
                self.events[event_id] = deepcopy(result)
            return result

    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    pm.analytics = ServerMergedLedger()
    pm.analytics.fail = True
    start = datetime(2026, 10, 23, 9, 31, tzinfo=IST)
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22525, event_time=start)
        await pm.update_trades(22470, event_time=start + timedelta(minutes=1))
        opt = trade["market_context"]["option_trailing"]
        opt["accounting"]["legs"]["T2"].update(option_price=160, option_source="quote", approximate=False)
        opt.update(phase="CLOSED", remaining_fraction=0, exit={"reason":"ATR_SL_HIT", "event_at":(start + timedelta(minutes=2)).isoformat()})
        trade["state"] = "CLOSED"
        pm._capture_reference_leg(trade, "EXIT", None, (start + timedelta(minutes=2)).isoformat(), option_close=155)
        await pm._record_option_transition(trade, None, "ATR_SL_HIT", (start + timedelta(minutes=2)).isoformat())
        pm.analytics.fail = False
        await pm.retry_option_lifecycle_writes()
    assert len(pm.analytics.rows) == 3
    assert trade["state"] == "CLOSED"
    assert opt["highest_milestone"] == 2
    assert opt["accounting"]["legs"]["T1"]["option_price"] == 150
    assert pm.analytics.rows[-1]["option_pnl_points"] == pytest.approx(.5*(150-139.25)+.3*(160-139.25)+.2*(155-139.25))
    assert pm.analytics.rows[-1]["alert_snapshot"]["option_trailing"]["accounting"]["legs"]["T1"]["option_price"] == 150


def test_delayed_premium_minute_fallback_uses_original_source_minute():
    pm = _make_pm()
    trade = reference_trade()
    trade["market_context"]["option_trailing"]["highest_milestone"] = 1
    at = datetime(2026, 10, 23, 9, 31, tzinfo=IST)
    pm._capture_reference_leg(trade, "T1", trade["target_1"], (at + timedelta(minutes=1)).isoformat(), at)
    row = {"timestamp": at.isoformat(), "sampled_at": (at + timedelta(minutes=1)).isoformat(),
           "complete": True, "open": 150, "high": 160, "low": 145, "close": 155}
    pm.set_reference_price_evidence(option_minutes={"51333": [row]})
    leg = pm._enrich_reference_trade(trade)["legs"]["T1"]
    assert leg["option_price"] == 155
    assert leg["option_source"] == "completed_1m_close"
    assert leg["approximate"] is True
