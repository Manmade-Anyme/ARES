import asyncio
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import pytest

import main as main_module
import position_manager as position_manager_module
from tests.unit.test_option_atr_lifecycle_comprehensive import _make_pm, _make_signal, _mock_trade_from_signal
from options_candles import OptionCandle

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
async def test_t1_only_ambiguity_resolution_persists_with_distinct_event_id():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    minute = datetime(2026, 10, 23, 9, 32, tzinfo=IST)
    ticks = [{"observed_at": (minute + timedelta(seconds=second)).isoformat(),
              "price": price, "source": "dhan_websocket_tick"}
             for second, price in [(10, 22560), (20, 22525)]]
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(22525, event_time=minute + timedelta(seconds=20))
        await pm.update_trades(22535, candle_high=22560, candle_low=22520,
            candle_timestamp=minute)
        assert trade["market_context"]["option_trailing"]["ambiguous_index_touch"]["kind"] == "T1_ENTRY"
        assert pm.analytics.rows[-1]["option_trailing"]["ambiguous_index_touch"] is not None
        feed = SimpleNamespace(get_complete_minute_observations=lambda _: (ticks, True))
        assert await pm.resolve_reference_ambiguities_from_feed(feed) == 1

    opt = trade["market_context"]["option_trailing"]
    assert trade["state"] == "T1_HIT"
    assert opt["ambiguity_resolution"]["outcome"] == "T1_ONLY"
    assert opt["ambiguous_index_touch"] is None
    assert len(pm.analytics.rows) == 3
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
async def test_atr_protection_does_not_wait_for_open_accounting_recovery(monkeypatch):
    pm = _make_pm()
    trade = reference_trade()
    trade["state"] = "T1_HIT"
    opt = trade["market_context"]["option_trailing"]
    start = datetime(2026, 10, 23, 8, 0, tzinfo=IST)
    prior = [OptionCandle(start + timedelta(minutes=5 * i), start + timedelta(minutes=5 * (i + 1)),
                          132, 135, 125, 132, 1, "51333") for i in range(19)]
    cutoff = prior[-1].end_time
    opt.update(phase="POST_T1", highest_milestone=1, t1_at="2026-10-23T09:30:00+05:30",
               last_processed_option_bar_end=cutoff.isoformat(), locked_option_sl=130.0,
               protection_mode="ATR", accounting={"legs": {"T1": {
                   "event_at": "2026-10-23T09:30:00+05:30", "spot_price": 22527.1,
                   "option_price": None,
               }}})
    pm.active_trades = [trade]
    pm._reference_price_evidence = {"option_quotes": {"51333": [{
        "observed_at": "2026-10-23T09:30:00+05:30", "price": 150.0,
        "source": "test_quote",
    }]}}

    entered, release = Event(), Event()
    ledger = pm.analytics
    original_commit = ledger.apply_option_lifecycle_transition

    def stalled_commit(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release stalled recovery write")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(ledger, "apply_option_lifecycle_transition", stalled_commit)
    sender = AsyncMock(return_value="DELIVERED")
    monkeypatch.setattr(position_manager_module, "send_option_lifecycle_event", sender)
    recovery = asyncio.create_task(pm.enrich_reference_accounting())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        exit_bar = OptionCandle(cutoff, cutoff + timedelta(minutes=5),
                                130, 131, 128, 129.95, 1, "51333")
        events = await asyncio.wait_for(
            pm.update_option_atr_trailing(prior + [exit_bar]), timeout=0.5)
        assert (trade["id"], "ATR_SL_HIT") in events
        assert trade["state"] == "CLOSED"
        assert opt["phase"] == "CLOSED"
        assert len(pm._option_pending[trade["id"]]) >= 2
    finally:
        release.set()
        await asyncio.wait_for(recovery, timeout=2)
        for task in list(getattr(pm, "_option_flush_tasks", {}).values()):
            await asyncio.wait_for(task, timeout=2)
    assert pm.analytics.rows[-1]["active_state"] == "CLOSED"
    assert pm.analytics.rows[-1]["update_type"] == "ATR_SL_HIT"
    assert pm.analytics.rows[-1]["score"] == 1


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
async def test_buffered_pre_t1_tick_cannot_hit_post_t1_entry_guard():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    minute = datetime(2026, 10, 23, 9, 31, tzinfo=IST)
    received_after_t1 = minute + timedelta(minutes=1, seconds=5)
    pre_t1_exchange_time = minute + timedelta(seconds=10)

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        t1_events = await pm.update_trades(
            22525.0, candle_high=22540.0, candle_low=22525.0,
            candle_timestamp=minute, event_time=minute + timedelta(minutes=1))
        committed_rows = len(pm.analytics.rows)
        replayed_events = await pm.update_trades(
            22553.0, event_time=received_after_t1,
            market_event_time=pre_t1_exchange_time)
        opt = trade["market_context"]["option_trailing"]
        assert t1_events == [(trade["id"], "T1_HIT")]
        assert opt["t1_at"] == (minute + timedelta(minutes=1)).astimezone(timezone.utc).isoformat()
        assert replayed_events == []
        assert trade["state"] == "T1_HIT"
        assert opt["phase"] == "POST_T1"
        assert opt["index_guard_enabled"] is True
        assert len(pm.analytics.rows) == committed_rows
        boundary_events = await pm.update_trades(
            22553.0, event_time=received_after_t1 + timedelta(seconds=2),
            market_event_time=minute + timedelta(minutes=1, seconds=1))

    assert boundary_events == [(trade["id"], "STOPPED_OUT_AT_BE")]
    assert trade["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_tick_t1_exchange_time_allows_later_exchange_t2_after_late_receipt():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    option = trade["market_context"]["option_trailing"]
    source_t1 = datetime(2026, 10, 23, 10, 0, 10, tzinfo=timezone.utc)
    received_t1 = source_t1 + timedelta(seconds=2)
    source_t2 = source_t1 + timedelta(seconds=1)
    received_t2 = source_t1 + timedelta(seconds=3)
    sign = 1 if trade["direction"] == "BULLISH" else -1

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        t1_events = await pm.update_trades(
            trade["target_1"], event_time=received_t1, market_event_time=source_t1)
        t2_events = await pm.update_trades(
            trade["target_2"], event_time=received_t2, market_event_time=source_t2)

    assert t1_events == [(trade["id"], "T1_HIT")]
    assert option["t1_at"] == source_t1.isoformat()
    assert option["accounting"]["legs"]["T1"]["event_at"] == source_t1.isoformat()
    assert pm.analytics.rows[0]["alert_snapshot"]["event_time"] == source_t1.isoformat()
    assert t2_events == [(trade["id"], "T2_HIT")]
    assert trade["state"] == "T2_REACHED"
    assert option["t2_at"] == source_t2.isoformat()
    assert option["accounting"]["legs"]["T2"]["event_at"] == source_t2.isoformat()
    assert pm.analytics.rows[-1]["alert_snapshot"]["event_time"] == source_t2.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("tick_offset,expected_score", [(-1, 2), (0, 1), (1, 1)])
async def test_live_spot_ticks_replay_by_source_time_without_guessing_same_second_order(tick_offset, expected_score):
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    option = trade["market_context"]["option_trailing"]
    option.update(phase="POST_T1", highest_milestone=1, t1_at="2026-10-23T10:00:00+05:30",
                  t1_activation_source="tick_exchange", locked_option_sl=150,
                  protection_mode="ATR", remaining_fraction=.5)
    trade.update(state="T1_HIT", stop_loss=trade["entry_price"])
    source_close = datetime(2026, 10, 23, 10, 55, tzinfo=IST)
    spot_tick_at = source_close + timedelta(seconds=tick_offset)
    option_start = datetime(2026, 10, 23, 9, 15, tzinfo=IST)
    history = [
        OptionCandle(option_start + timedelta(minutes=5 * i),
                     option_start + timedelta(minutes=5 * (i + 1)),
                         200, 201, 148 if i == 19 else 199,
                         149 if i == 19 else 200, 1, "51333")
        for i in range(20)
    ]

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_option_market_events(
            [], {"51333": history}, current_time=source_close + timedelta(seconds=5),
            index_ticks=[{"price": trade["target_2"], "exchange_at": spot_tick_at,
                          "received_at": source_close + timedelta(seconds=6), "source": "dhan_tick"}],
        )

    assert option["highest_milestone"] == expected_score
    assert pm.analytics.rows[-1]["score"] == expected_score
    if tick_offset == 0:
        assert trade["state"] == "T1_HIT"
        assert option.get("exit") is None
        ambiguity = option["ambiguous_index_touch"]
        assert ambiguity["kind"] == "SPOT_OPTION_EXIT_TIE"
        assert ambiguity["status"] == "ORDER_UNKNOWN"
        assert ambiguity["touches_t2"] is True
        protective_exit = ambiguity["protective_option_exit"]
        assert protective_exit["event_at"] == source_close.astimezone(timezone.utc).isoformat()
        assert protective_exit["option_close"] == 149
        assert protective_exit["option_stop"] > protective_exit["option_close"]
        assert protective_exit["reason"] == "ATR_SL_HIT"
        assert pm.analytics.rows[-1]["active_state"] == "T1_HIT"
        assert pm.analytics.rows[-1]["option_trailing"]["ambiguous_index_touch"] == ambiguity
    else:
        assert trade["state"] == "CLOSED"
        assert option["exit"]["reason"] == "ATR_SL_HIT"
        assert option["exit"]["event_at"] == source_close.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("option_close_time,t1_time,processed_time", [
    ("10:55", "10:56", None),
    ("11:00", "10:56", "11:00"),
])
async def test_same_close_ambiguity_ignores_pre_t1_or_processed_bars(option_close_time, t1_time, processed_time):
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    option = trade["market_context"]["option_trailing"]
    day = datetime(2026, 10, 23, tzinfo=IST)
    option_close = day.replace(hour=int(option_close_time[:2]), minute=int(option_close_time[3:]))
    t1_at = day.replace(hour=int(t1_time[:2]), minute=int(t1_time[3:]))
    option.update(phase="POST_T1", highest_milestone=1, t1_at=t1_at.isoformat(),
                  t1_activation_source="tick_exchange", locked_option_sl=150,
                  protection_mode="ATR", remaining_fraction=.5)
    if processed_time:
        option["last_processed_option_bar_end"] = day.replace(
            hour=int(processed_time[:2]), minute=int(processed_time[3:])).isoformat()
    trade.update(state="T1_HIT", stop_loss=trade["entry_price"])
    bar = SimpleNamespace(end_time=option_close, close=149)
    tick = {"price": trade["target_2"], "exchange_at": option_close,
            "received_at": option_close + timedelta(seconds=5)}

    await pm._mark_same_close_option_spot_ambiguities(
        option_close, {option_close: []}, {}, {option_close: [tick]},
        {trade["id"]: [bar]},
    )

    assert option.get("ambiguous_index_touch") is None
    sign = 1 if trade["direction"] == "BULLISH" else -1
    guard_at = max(option_close, t1_at) + timedelta(seconds=30)
    pm._option_available_until = {trade["id"]: guard_at + timedelta(minutes=10)}
    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        events = await pm.update_trades(
            trade["entry_price"] - sign, event_time=guard_at, market_event_time=guard_at)

    assert events == [(trade["id"], "STOPPED_OUT_AT_BE")]
    assert trade["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_delayed_entry_guard_exit_persists_source_and_receipt_times_separately():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    option = trade["market_context"]["option_trailing"]
    source_t1 = datetime(2026, 10, 23, 10, 0, 10, tzinfo=timezone.utc)
    source_guard = source_t1 + timedelta(seconds=2)
    received_guard = source_guard + timedelta(seconds=10)
    sign = 1 if trade["direction"] == "BULLISH" else -1

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(
            trade["target_1"], event_time=source_t1 + timedelta(seconds=2),
            market_event_time=source_t1)
        events = await pm.update_trades(
            trade["entry_price"] - sign, event_time=received_guard,
            market_event_time=source_guard)

    assert events == [(trade["id"], "STOPPED_OUT_AT_BE")]
    assert option["exit"]["event_at"] == source_guard.isoformat()
    assert option["exit"]["received_at"] == received_guard.isoformat()
    assert pm.analytics.rows[-1]["exit_timestamp"] == source_guard.isoformat()
    assert pm.analytics.rows[-1]["alert_snapshot"]["event_time"] == source_guard.isoformat()


@pytest.mark.asyncio
async def test_tick_loop_sends_buffered_option_ticks_to_chronological_replay(monkeypatch):
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    pm.record_reference_spot_quote = MagicMock()
    pm.update_trades = AsyncMock(return_value=[])
    tick = {"price": 22470.0,
            "exchange_at": datetime(2026, 10, 23, 10, 54, 59, tzinfo=IST),
            "received_at": datetime(2026, 10, 23, 10, 55, 4, tzinfo=IST),
            "source": "dhan_tick"}
    feed = SimpleNamespace(is_active=True, get_latest_price=lambda: tick["price"],
                           get_observations_since=lambda: [tick])
    fetcher = SimpleNamespace(watch_candles=[])
    monkeypatch.setattr(main_module.asyncio, "sleep", AsyncMock())
    replay = AsyncMock(return_value=True)
    monkeypatch.setattr(main_module, "_monitor_completed_atr", replay)
    monkeypatch.setattr(main_module, "_schedule_reference_recovery", lambda *args: None)
    monkeypatch.setattr(main_module, "_resolve_reference_ambiguities", AsyncMock())

    await main_module._sleep_with_tick_exits(
        0.01, feed, pm, SimpleNamespace(clear_cooldown=lambda: None),
        price_fetcher=fetcher,
        respect_close=False)

    replay.assert_awaited_once()
    assert replay.await_args.kwargs["index_ticks"] == [tick]
    pm.record_reference_spot_quote.assert_called_once_with(22470.0, tick["exchange_at"], "dhan_tick")
    pm.update_trades.assert_awaited_once_with(
        22470.0, event_time=tick["received_at"], market_event_time=tick["exchange_at"],
        include_option_policy=False)


@pytest.mark.asyncio
async def test_completed_candle_t1_same_second_guard_stays_unresolved():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    minute = datetime(2026, 10, 23, 9, 31, tzinfo=IST)
    boundary = minute + timedelta(minutes=1)

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(
            22525.0, candle_high=22540.0, candle_low=22525.0,
            candle_timestamp=minute, event_time=boundary)
        boundary_events = await pm.update_trades(
            22553.0, event_time=boundary + timedelta(seconds=5), market_event_time=boundary)
        option = trade["market_context"]["option_trailing"]
        ticks = [{"observed_at": boundary.isoformat(), "price": 22553.0,
                  "source": "complete_tick_history"}]
        resolved = await pm.resolve_reference_ambiguity(trade["id"], ticks, complete=True)

    assert boundary_events == []
    assert not resolved
    assert trade["state"] == "T1_HIT"
    assert option["t1_activation_source"] == "completed_candle"
    assert option["ambiguous_index_touch"]["kind"] == "T1_PHASE_ENTRY"
    assert option["ambiguous_index_touch"]["status"] == "ORDER_UNKNOWN"


@pytest.mark.asyncio
async def test_same_exchange_second_t1_and_entry_guard_preserves_ambiguity():
    pm = _make_pm()
    trade = reference_trade()
    pm.active_trades = [trade]
    option = trade["market_context"]["option_trailing"]
    source_t1 = datetime(2026, 10, 23, 10, 0, 10, tzinfo=timezone.utc)
    sign = 1 if trade["direction"] == "BULLISH" else -1

    with patch("position_manager.send_option_lifecycle_event", new=AsyncMock(return_value="DELIVERED")):
        await pm.update_trades(
            trade["target_1"], event_time=source_t1 + timedelta(seconds=2),
            market_event_time=source_t1)
        guard_events = await pm.update_trades(
            trade["entry_price"] - sign, event_time=source_t1 + timedelta(seconds=3),
            market_event_time=source_t1)
        observations = [
            {"observed_at": source_t1.isoformat(), "price": trade["target_1"],
             "source": "provider_certified_order"},
            {"observed_at": source_t1.isoformat(), "price": trade["entry_price"] - sign,
             "source": "provider_certified_order"},
        ]
        unresolved = await pm.resolve_reference_ambiguity(
            trade["id"], observations, complete=False)
        resolved = await pm.resolve_reference_ambiguity(
            trade["id"], observations, complete=True)

    assert guard_events == []
    assert not unresolved
    assert resolved
    assert trade["state"] == "CLOSED"
    assert option["exit"]["reason"] == "STOPPED_OUT_AT_BE"
    assert option["exit"]["received_at"] == (source_t1 + timedelta(seconds=3)).isoformat()
    assert pm.analytics.rows[-1]["exit_timestamp"] == source_t1.isoformat()
    assert option["ambiguity_resolution"]["outcome"] == "STOPPED_OUT_AT_BE"
    assert option["ambiguous_index_touch"] is None
    restored = position_manager_module.OptionTrailingContext.from_dict(option).to_dict()
    assert restored["t1_activation_source"] == "tick_exchange"
    assert restored["ambiguous_index_touch"] is None


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
