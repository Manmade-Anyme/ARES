"""Public ingestion and application monitoring regressions; no live requests."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from options_candles import IST, OptionCandle, contiguous_option_candles, load_option_candles
from atr_trailing import calculate_option_atr_bars
from fetchers.price_fetcher import PriceFetcher


def row(at, **kwargs):
    return dict(timestamp=at, open=100, high=110, low=95, close=105,
                security_id="51333", **kwargs)


def test_native_objects_revalidated_and_explicit_end_respected():
    now = datetime(2026, 10, 23, 10, 0, tzinfo=IST)
    invalid = OptionCandle(now, now + timedelta(minutes=5), 100, 110, 95, 105, 0, "51331")
    assert load_option_candles([invalid], observed_at=now, expected_security_id="51333") == []
    assert load_option_candles([row("2026-10-23T09:15:00+05:30", end="2026-10-23T09:16:00+05:30")],
                               observed_at=now, expected_security_id="51333") == []
    assert len(load_option_candles([row("2026-10-23T09:15:00+05:30")],
                                   observed_at=now, expected_security_id="51333")) == 1


def test_gap_blocks_subsequent_bars_until_backfilled_without_overnight_reset():
    raw = [row("2026-10-22T15:25:00+05:30"), row("2026-10-23T09:15:00+05:30"),
           row("2026-10-23T09:25:00+05:30")]
    bars = load_option_candles(raw)
    assert len(contiguous_option_candles(bars)) == 2
    assert len(calculate_option_atr_bars(bars)) == 2
    recovered = load_option_candles(raw + [row("2026-10-23T09:20:00+05:30")])
    assert len(contiguous_option_candles(recovered)) == 4


@pytest.mark.asyncio
async def test_fetch_pinned_native_history_uses_multiday_and_filters_forming():
    fetcher = PriceFetcher.__new__(PriceFetcher)
    seen = []
    def provider(*args, **kwargs):
        seen.append((args, kwargs))
        return {"status": "success", "data": {
            "timestamp": ["2026-10-22T15:25:00+05:30", "2026-10-23T09:15:00+05:30", "2026-10-23T09:20:00+05:30"],
            "open": [100]*3, "high": [110]*3, "low": [95]*3, "close": [105]*3, "volume": [1]*3}}
    fetcher.dhan = SimpleNamespace(intraday_minute_data=provider)
    now = datetime(2026, 10, 23, 9, 22, tzinfo=IST)
    contract = dict(security_id="51333", exchange="NSE_FNO", expiry="2026-10-27", strike=22550, option_type="PE",
                    history_from="2026-10-13")
    bars = await fetcher.fetch_option_candles(contract, observed_at=now)
    assert len(bars) == 2
    assert seen[0][0][:3] == ("51333", "NSE_FNO", "OPTIDX")
    assert seen[0][0][3] == "2026-10-13"
    assert seen[0][1]["interval"] == 5
    assert await fetcher.fetch_option_candles(contract, observed_at=now+timedelta(seconds=20)) == bars
    assert len(seen) == 1  # Healthy history reused within this completed-close interval.
    await fetcher.fetch_option_candles(contract, observed_at=now+timedelta(minutes=10))
    await fetcher.fetch_option_candles(contract, observed_at=now+timedelta(minutes=10, seconds=1))
    assert len(seen) == 3  # Missing latest bars must keep retrying, never cache a gap.
    assert all(args[3] == "2026-10-13" for args, _ in seen)


@pytest.mark.asyncio
async def test_application_monitor_fetches_options_when_index_history_empty_and_isolates_failure():
    import main
    now = datetime(2026, 10, 23, 10, 0, tzinfo=IST)
    contract = dict(security_id="51333", exchange="NSE_FNO", expiry="2026-10-27", strike=22550, option_type="PE")
    positions = SimpleNamespace(active_trades=[dict(state="T1_HIT", market_context={"option_trailing": {
        "policy_version": "option_atr_v1", "contract": contract}})],
        update_atr_trailing=AsyncMock(side_effect=RuntimeError("legacy failed")),
        update_option_market_events=AsyncMock())
    dispatcher = SimpleNamespace(on_candles=AsyncMock())
    fetcher = SimpleNamespace(fetch_option_candles=AsyncMock(return_value=[]))
    await main._monitor_completed_atr([], positions, dispatcher, price_fetcher=fetcher, current_time=now)
    assert positions.update_option_market_events.await_args.args == ([], {"51333": []})
    assert positions.update_option_market_events.await_args.kwargs["current_time"] == now
    dispatcher.on_candles.assert_awaited_once()


@pytest.mark.asyncio
async def test_v2_monitor_polls_pinned_premium_quotes_even_when_atr_is_disabled():
    import main
    now = datetime(2026, 11, 4, 10, 0, tzinfo=IST)
    contract = dict(security_id="39961", exchange_segment="NSE_FNO", expiry="2026-11-09",
                    strike=23950, option_type="CE")
    quote = {"contract": contract, "price": 167.0, "observed_at": now.isoformat(),
             "received_at": now.isoformat(), "source": "dhan_last_trade"}
    positions = SimpleNamespace(
        active_trades=[dict(state="T1_HIT", market_context={"option_trailing": {
            "policy_version": "option_premium_protection_v2", "atr_enabled": False,
            "contract": contract}})],
        update_atr_trailing=AsyncMock(), update_option_market_events=AsyncMock(return_value=[]),
        mark_option_quote_unavailable=AsyncMock())
    dispatcher = SimpleNamespace(on_candles=AsyncMock())
    fetcher = SimpleNamespace(fetch_option_observations=AsyncMock(return_value=[quote]),
                              fetch_option_candles=AsyncMock(return_value=[]))

    await main._monitor_completed_atr([], positions, dispatcher, price_fetcher=fetcher,
                                      current_time=now)

    fetcher.fetch_option_observations.assert_awaited_once_with([contract])
    fetcher.fetch_option_candles.assert_not_awaited()
    positions.update_option_market_events.assert_awaited_once()
    assert positions.update_option_market_events.await_args.kwargs["option_quotes"] == [quote]
    assert positions.update_option_market_events.await_args.kwargs["quote_freshness_at"] == now
    positions.mark_option_quote_unavailable.assert_not_awaited()


@pytest.mark.asyncio
async def test_v2_quote_freshness_is_anchored_before_slow_history_fetch(monkeypatch):
    import main
    request_at = datetime(2026, 11, 4, 10, 20, tzinfo=IST)
    quote_at = request_at + timedelta(seconds=2)
    history_done_at = request_at + timedelta(seconds=12)
    clock = iter((request_at, quote_at, history_done_at))

    class Clock:
        @classmethod
        def now(cls, tz=None):
            return next(clock)

    monkeypatch.setattr(main, "datetime", Clock)
    contract = dict(security_id="39961", exchange_segment="NSE_FNO", expiry="2026-11-09",
                    strike=23950, option_type="CE")
    quote = {"contract": contract, "price": 167.0,
             "observed_at": (quote_at - timedelta(seconds=1)).isoformat(),
             "received_at": quote_at.isoformat(), "source": "dhan_last_trade"}
    positions = SimpleNamespace(
        active_trades=[dict(state="T1_HIT", market_context={"option_trailing": {
            "policy_version": "option_premium_protection_v2", "atr_enabled": True,
            "contract": contract}})],
        update_atr_trailing=AsyncMock(), update_option_market_events=AsyncMock(return_value=[]),
        mark_option_quote_unavailable=AsyncMock())
    fetcher = SimpleNamespace(
        fetch_option_observations=AsyncMock(return_value=[quote]),
        fetch_option_candles=AsyncMock(return_value=[]))

    await main._monitor_completed_atr([], positions, SimpleNamespace(on_candles=AsyncMock()),
                                      price_fetcher=fetcher)

    assert fetcher.fetch_option_candles.await_args.kwargs["observed_at"] == request_at
    kwargs = positions.update_option_market_events.await_args.kwargs
    assert kwargs["quote_freshness_at"] == quote_at
    assert kwargs["current_time"] == history_done_at


@pytest.mark.asyncio
async def test_v2_monitor_marks_quote_unavailable_when_protective_poll_fails():
    import main
    from fetchers.price_fetcher import ReferencePriceDeferred
    now = datetime(2026, 11, 4, 10, 0, tzinfo=IST)
    contract = dict(security_id="39961", exchange_segment="NSE_FNO", expiry="2026-11-09",
                    strike=23950, option_type="CE")
    positions = SimpleNamespace(
        active_trades=[dict(state="T1_HIT", market_context={"option_trailing": {
            "policy_version": "option_premium_protection_v2", "atr_enabled": False,
            "contract": contract}})],
        update_atr_trailing=AsyncMock(), update_option_market_events=AsyncMock(return_value=[]),
        mark_option_quote_unavailable=AsyncMock())
    dispatcher = SimpleNamespace(on_candles=AsyncMock())
    fetcher = SimpleNamespace(fetch_option_observations=AsyncMock(side_effect=ReferencePriceDeferred("paced")),
                              fetch_option_candles=AsyncMock(return_value=[]))

    await main._monitor_completed_atr([], positions, dispatcher, price_fetcher=fetcher,
                                      current_time=now)

    positions.mark_option_quote_unavailable.assert_awaited_once_with(
        {"39961": contract}, "No fresh quote for the pinned contract", now)
    assert positions.update_option_market_events.await_args.kwargs["option_quotes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "stop_domain", "milestone"), [
    ("ATR_SL_HIT", "OPTION_PREMIUM", 1),
    ("STOPPED_OUT_AT_BE", "SPOT_INDEX", 1),
    ("SL_HIT", "SPOT_INDEX", 0),
])
async def test_v2_terminal_premium_or_index_stop_event_is_routed_to_execution_dispatcher(
        reason, stop_domain, milestone):
    import main
    now = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    contract = dict(security_id="39961", exchange_segment="NSE_FNO", expiry="2026-11-09",
                    strike=23950, option_type="CE")
    trade = {"id": "v2-trade", "market_context": {"option_trailing": {
        "policy_version": "option_premium_protection_v2",
        "highest_milestone": milestone,
        "contract": contract,
        "exit": {"reason": reason, "stop_domain": stop_domain},
    }}}
    positions = SimpleNamespace(
        active_trades=[trade],
        update_atr_trailing=AsyncMock(),
        update_option_market_events=AsyncMock(return_value=[("v2-trade", reason)]),
        mark_option_quote_unavailable=AsyncMock())
    dispatcher = SimpleNamespace(on_candles=AsyncMock(), on_option_stop=AsyncMock())
    fetcher = SimpleNamespace(fetch_option_observations=AsyncMock(return_value=[]),
                              fetch_option_candles=AsyncMock(return_value=[]))

    await main._monitor_completed_atr([], positions, dispatcher, price_fetcher=fetcher,
                                      current_time=now)

    dispatcher.on_option_stop.assert_awaited_once_with(trade, reason)


@pytest.mark.asyncio
async def test_resolved_v2_index_guard_exit_is_routed_from_durable_pending_marker():
    import main
    now = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    reason = "STOPPED_OUT_AT_BE"
    trade = {"id": "v2-trade", "state": "CLOSED", "market_context": {"option_trailing": {
        "policy_version": "option_premium_protection_v2", "highest_milestone": 1,
        "exit": {"reason": reason, "stop_domain": "SPOT_INDEX"},
        "execution_route_pending": True, "execution_route_reason": reason,
    }}}
    positions = SimpleNamespace(active_trades=[trade], update_atr_trailing=AsyncMock(),
                                update_option_market_events=AsyncMock(return_value=[]),
                                mark_option_quote_unavailable=AsyncMock())
    dispatcher = SimpleNamespace(on_candles=AsyncMock(), on_option_stop=AsyncMock())

    await main._monitor_completed_atr([], positions, dispatcher, current_time=now)

    dispatcher.on_option_stop.assert_awaited_once_with(trade, reason)


@pytest.mark.asyncio
async def test_v2_deferred_premium_stop_is_routed_while_index_ambiguity_is_open():
    import main
    now = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    contract = dict(security_id="39961", exchange_segment="NSE_FNO", expiry="2026-11-09",
                    strike=23950, option_type="CE")
    reason = "STOPPED_OUT_AT_BE"
    trade = {"id": "v2-trade", "state": "T1_HIT", "market_context": {"option_trailing": {
        "policy_version": "option_premium_protection_v2", "highest_milestone": 1,
        "contract": contract, "exit": None,
        "ambiguous_index_touch": {"kind": "T2_ENTRY", "status": "ORDER_UNKNOWN",
                                  "protective_option_exit": {"reason": reason,
                                      "stop_domain": "OPTION_PREMIUM"}},
    }}}
    positions = SimpleNamespace(
        active_trades=[trade], update_atr_trailing=AsyncMock(),
        update_option_market_events=AsyncMock(return_value=[("v2-trade", reason)]),
        mark_option_quote_unavailable=AsyncMock())
    dispatcher = SimpleNamespace(on_candles=AsyncMock(), on_option_stop=AsyncMock())
    fetcher = SimpleNamespace(fetch_option_observations=AsyncMock(return_value=[]),
                              fetch_option_candles=AsyncMock(return_value=[]))

    await main._monitor_completed_atr([], positions, dispatcher, price_fetcher=fetcher,
                                      current_time=now)

    dispatcher.on_option_stop.assert_awaited_once_with(trade, reason)


@pytest.mark.asyncio
async def test_v2_premium_stop_is_routed_while_index_history_replay_is_pending():
    import main
    now = datetime(2026, 11, 4, 10, 21, tzinfo=IST)
    contract = dict(security_id="39961", exchange_segment="NSE_FNO", expiry="2026-11-09",
                    strike=23950, option_type="CE")
    reason = "STOPPED_OUT_AT_BE"
    trade = {"id": "v2-trade", "state": "T1_HIT", "market_context": {"option_trailing": {
        "policy_version": "option_premium_protection_v2", "highest_milestone": 1,
        "contract": contract, "exit": None,
        "deferred_option_exit": {"reason": reason, "stop_domain": "OPTION_PREMIUM"},
    }}}
    positions = SimpleNamespace(
        active_trades=[trade], update_atr_trailing=AsyncMock(),
        update_option_market_events=AsyncMock(return_value=[("v2-trade", reason)]),
        mark_option_quote_unavailable=AsyncMock())
    dispatcher = SimpleNamespace(on_candles=AsyncMock(), on_option_stop=AsyncMock())
    fetcher = SimpleNamespace(fetch_option_observations=AsyncMock(return_value=[]),
                              fetch_option_candles=AsyncMock(return_value=[]))

    await main._monitor_completed_atr([], positions, dispatcher, price_fetcher=fetcher,
                                      current_time=now)

    dispatcher.on_option_stop.assert_awaited_once_with(trade, reason)


@pytest.mark.asyncio
async def test_monitor_routes_each_pending_exit_and_retries_failed_sync_on_later_pass():
    import main
    contract = dict(security_id="39961", exchange_segment="NSE_FNO", expiry="2026-11-09",
                    strike=23950, option_type="CE")
    trades = []
    for trade_id in ("first", "second"):
        trades.append({"id": trade_id, "state": "CLOSED", "market_context": {"option_trailing": {
            "policy_version": "option_premium_protection_v2", "highest_milestone": 1,
            "contract": contract, "exit": {"reason": "ATR_SL_HIT", "stop_domain": "OPTION_PREMIUM"},
            "execution_route_pending": True, "execution_route_reason": "ATR_SL_HIT",
        }}})
    trades[0]["market_context"]["execution"] = ["invalid"]

    async def acknowledge(trade_id, reason):
        trade = next(item for item in trades if item["id"] == trade_id)
        option = trade["market_context"]["option_trailing"]
        assert option["execution_route_reason"] == reason
        option["execution_route_pending"] = False

    positions = SimpleNamespace(active_trades=[], _option_execution_route_trades=trades,
                                update_atr_trailing=AsyncMock(),
                                acknowledge_option_execution_route=AsyncMock(side_effect=acknowledge))
    dispatcher = SimpleNamespace(on_candles=AsyncMock(),
                                 on_option_stop=AsyncMock(side_effect=[RuntimeError("sync failed"), True, True]))

    await main._monitor_completed_atr([], positions, dispatcher, current_time=datetime(2026, 11, 4, 10, 21, tzinfo=IST))

    assert [call.args[0]["id"] for call in dispatcher.on_option_stop.await_args_list] == ["first", "second"]
    assert trades[0]["market_context"]["option_trailing"]["execution_route_pending"] is True
    assert trades[1]["market_context"]["option_trailing"]["execution_route_pending"] is False

    await main._monitor_completed_atr([], positions, dispatcher, current_time=datetime(2026, 11, 4, 10, 22, tzinfo=IST))

    assert [call.args[0]["id"] for call in dispatcher.on_option_stop.await_args_list] == [
        "first", "second", "first"]
    assert all(not trade["market_context"]["option_trailing"]["execution_route_pending"] for trade in trades)


@pytest.mark.asyncio
async def test_monitor_resumes_terminal_paper_exit_after_route_marker_was_acknowledged():
    import main
    trade = {"id": "terminal-pending-fill", "state": "CLOSED", "market_context": {
        "execution": {"mode": "PAPER", "status": "EXIT_PENDING",
                      "pending_exit_quantity": 65, "exit_reason": "ATR_SL_HIT"},
        "option_trailing": {
            "policy_version": "option_premium_protection_v2",
            "execution_status": "CONFIRMED_PAPER_FILL",
            "execution_route_pending": False,
            "exit": {"reason": "ATR_SL_HIT", "stop_domain": "OPTION_PREMIUM"},
        },
    }}
    missing_reason = {"id": "pending-without-reason", "state": "CLOSED", "market_context": {
        "execution": {"mode": "PAPER", "status": "EXIT_PENDING"},
        "option_trailing": {
            "policy_version": "option_premium_protection_v2",
            "execution_status": "CONFIRMED_PAPER_FILL",
            "execution_route_pending": False,
            "exit": {"reason": "ATR_SL_HIT", "stop_domain": "OPTION_PREMIUM"},
        },
    }}
    positions = SimpleNamespace(
        active_trades=[], _option_execution_route_trades=[trade, missing_reason],
        update_atr_trailing=AsyncMock(), update_option_market_events=AsyncMock(),
    )
    dispatcher = SimpleNamespace(on_candles=AsyncMock(), on_option_stop=AsyncMock(return_value=True))

    await main._monitor_completed_atr([], positions, dispatcher,
                                      current_time=datetime(2026, 11, 4, 10, 22, tzinfo=IST))

    dispatcher.on_option_stop.assert_awaited_once_with(trade, "ATR_SL_HIT")


@pytest.mark.asyncio
async def test_index_request_failure_still_runs_option_protection():
    import main
    contract = dict(security_id="51333", exchange="NSE_FNO", expiry="2026-10-27", strike=22550, option_type="PE")
    positions = SimpleNamespace(active_trades=[dict(state="T1_HIT", market_context={"option_trailing": {
        "policy_version": "option_atr_v1", "contract": contract}})],
        update_atr_trailing=AsyncMock(), update_option_market_events=AsyncMock())
    fetcher = SimpleNamespace(watch_candles=[], fetch_option_candles=AsyncMock(return_value=[]),
                              fetch_latest_candle=AsyncMock(side_effect=ValueError("index offline")))
    dispatcher = SimpleNamespace(on_candles=AsyncMock())
    with pytest.raises(ValueError, match="index offline"):
        await main._fetch_candle_with_protection(fetcher, positions, dispatcher)
    positions.update_option_market_events.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_paper_atr_cannot_compete_with_option_policy():
    from execution.signal_dispatch import SignalDispatcher
    from unittest.mock import Mock
    signal = SimpleNamespace(id="signal-1", market_context={})
    positions = SimpleNamespace(active_trades=[dict(signal_uuid="signal-1", market_context={
        "option_trailing": {"policy_version": "option_atr_v1"}})])
    orders = SimpleNamespace(broker=None, on_candles=Mock(side_effect=AssertionError("competing ATR")))
    dispatcher = SignalDispatcher(orders, positions)
    dispatcher._signals["correlation-1"] = (signal, None)
    await dispatcher.on_candles([])
    orders.on_candles.assert_not_called()


def test_indicator_keeps_full_precision_in_internal_stop():
    bars = load_option_candles([
        row((datetime(2026, 10, 23, 9, 15, tzinfo=IST) + timedelta(minutes=5*i)).isoformat())
        for i in range(20)])
    values = calculate_option_atr_bars(bars, initial_atr=1.123456789)
    expected = 1.123456789
    for _ in bars:
        expected = (expected * 4 + 15) / 5
    assert values[-1].atr == expected
    assert values[-1].atr != round(expected, 6)


@pytest.mark.asyncio
async def test_tick_and_rest_catch_up_option_closes_before_current_index_observation(monkeypatch):
    import main
    order = []
    async def catch_up(*args, **kwargs):
        order.append("option_catchup")
        return []
    async def current_spot(*args, **kwargs):
        order.append("current_index")
        return []
    contract = dict(security_id="51333", exchange_segment="NSE_FNO", expiry="2026-10-27", strike=22550, option_type="PE")
    positions = SimpleNamespace(active_trades=[dict(state="T1_HIT", market_context={"option_trailing": {
        "policy_version": "option_atr_v1", "contract": contract}})],
        update_atr_trailing=AsyncMock(), update_option_market_events=catch_up, update_trades=current_spot)
    fetcher = SimpleNamespace(watch_candles=[], fetch_option_candles=AsyncMock(return_value=[]),
                              fetch_latest_spot=AsyncMock(return_value=22470))
    dispatcher = SimpleNamespace(on_candles=AsyncMock(), on_spot=AsyncMock(), on_snapshot=AsyncMock())
    feed = SimpleNamespace(is_active=True, get_latest_price=lambda:22470)
    engine = SimpleNamespace(clear_cooldown=lambda: None)
    monkeypatch.setattr(main.settings, "tick_exit_check_interval_seconds", 1)
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    await main._sleep_with_tick_exits(1, feed, positions, engine, dispatcher, price_fetcher=fetcher)
    assert order == ["option_catchup", "current_index"]
    order.clear()
    oi = SimpleNamespace(get_nearest_expiry=AsyncMock(side_effect=ValueError("chain offline")))
    monkeypatch.setattr(main, "_session_now", lambda:datetime(2026,10,23,10))
    with pytest.raises(ValueError, match="chain offline"):
        await main._monitor_rest_without_candle(fetcher, oi, positions, engine, dispatcher)
    assert order == ["option_catchup", "current_index"]
