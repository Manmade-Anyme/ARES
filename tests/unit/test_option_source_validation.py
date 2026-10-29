"""Review regressions for source validity and phase activation chronology."""
from datetime import datetime, timedelta

import pytest

from options_candles import IST, OptionCandle
from tests.unit.test_option_lifecycle_commit_boundary import make_pm


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"complete":False}, {"close":float("nan")}, {"high":float("inf")},
    {"low":-1}, {"high":22400}, {"timestamp":"invalid-time"},
])
async def test_invalid_index_source_never_advances_milestones(monkeypatch,bad):
    pm,trade,sender=make_pm(monkeypatch)
    row={"timestamp":"2026-10-23T10:15:00+05:30","open":22540,"high":22550,
         "low":22470,"close":22470,"complete":True,**bad}
    await pm.update_option_market_events([row],{},current_time=datetime(2026,10,23,10,16,tzinfo=IST))
    assert trade["state"] == "OPEN"
    assert not pm.analytics.rows


def option_history():
    start=datetime(2026,10,23,9,15,tzinfo=IST)
    return [OptionCandle(start+timedelta(minutes=5*i),start+timedelta(minutes=5*(i+1)),
                         100,101,99,100,1,"51333") for i in range(11)]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_start",["2026-10-23T10:08:00+05:30","2026-10-23T10:09:00+05:30"])
async def test_index_guard_never_replays_wicks_before_tick_t1_activation(monkeypatch,source_start):
    pm,trade,sender=make_pm(monkeypatch)
    await pm.update_trades(22525,event_time=datetime(2026,10,23,10,9,19,tzinfo=IST))
    assert trade["state"] == "T1_HIT"
    row={"timestamp":source_start,"open":22540,"high":22555,
         "low":22520,"close":22525,"complete":True}
    await pm.update_option_market_events([row],{"51333":option_history()},
                                         current_time=datetime(2026,10,23,10,10,tzinfo=IST))
    assert trade["state"] == "T1_HIT"
    assert trade["market_context"]["option_trailing"]["highest_milestone"] == 1


def carried_runner(trade):
    trade.update(state="T1_HIT", stop_loss=trade["entry_price"], entry_timestamp="2026-10-22T10:08:38+05:30")
    opt=trade["market_context"]["option_trailing"]
    opt.update(phase="POST_T1",highest_milestone=1,booked_fraction=.5,remaining_fraction=.5,
               t1_at="2026-10-22T10:10:00+05:30",locked_option_sl=196,protection_mode="ATR",
               last_processed_option_bar_end="2026-10-22T15:30:00+05:30")
    return opt


@pytest.mark.parametrize('ingestion', ['market', 'premium_then_index'])
@pytest.mark.parametrize('missing_close', [120, 200])
@pytest.mark.asyncio
async def test_truncated_history_queues_t2_until_missing_option_close_is_recovered(monkeypatch, ingestion, missing_close):
    pm, trade, sender = make_pm(monkeypatch)
    trade.update(state='T1_HIT', stop_loss=trade['entry_price'])
    opt = trade['market_context']['option_trailing']
    opt.update(phase='POST_T1', highest_milestone=1, booked_fraction=.5, remaining_fraction=.5,
               t1_at='2026-10-23T10:09:00+05:30', locked_option_sl=127, protection_mode='ATR',
               last_processed_option_bar_end='2026-10-23T10:50:00+05:30')
    end = datetime(2026, 10, 23, 11, tzinfo=IST)
    truncated = OptionCandle(end-timedelta(minutes=5), end, 200, 201, 199, 200, 1, '51333')
    touch_at = end+timedelta(minutes=1)
    index = {'timestamp': end.isoformat(), 'open': 22520, 'high': 22520,
             'low': 22470, 'close': 22470, 'complete': True}
    if ingestion == 'market':
        await pm.update_option_market_events([index], {'51333': [truncated]}, current_time=touch_at)
    else:
        await pm.update_option_atr_trailing({'51333': [truncated]}, current_time=touch_at)
        await pm.update_trades(22470, event_time=touch_at)
    assert trade['state'] == 'T1_HIT'
    assert opt['highest_milestone'] == 1
    assert datetime.fromisoformat(opt['last_processed_option_bar_end']) == end-timedelta(minutes=10)
    assert opt['pending_index_observations']
    assert not [call for call in sender.call_args_list if call.args[0]['update_type'] == 'T2_HIT']

    missing = OptionCandle(end-timedelta(minutes=10), end-timedelta(minutes=5),
                          200, 201, min(199, missing_close-1), missing_close, 1, '51333')
    await pm.update_option_market_events([], {'51333': [missing, truncated]}, current_time=touch_at)
    if missing_close < 127:
        assert trade['state'] == 'CLOSED'
        assert pm.analytics.rows[-1]['score'] == 1
        assert opt['exit']['reason'] == 'ATR_SL_HIT'
        assert datetime.fromisoformat(opt['exit']['event_at']) == missing.end
        assert opt['exit']['exited_fraction'] == .5
        assert not [call for call in sender.call_args_list if call.args[0]['update_type'] == 'T2_HIT']
    else:
        assert trade['state'] == 'T2_REACHED'
        assert pm.analytics.rows[-1]['score'] == 2
        assert opt['remaining_fraction'] == .2
    assert opt['pending_index_observations'] == []


@pytest.mark.parametrize('watermark,start', [
    ('2026-10-23T10:50:00+05:30', '2026-10-23T10:50:00+05:30'),
    ('2026-10-22T15:30:00+05:30', '2026-10-23T09:15:00+05:30'),
])
@pytest.mark.asyncio
async def test_adjacent_restored_history_remains_eligible_across_session_boundary(monkeypatch, watermark, start):
    pm, trade, _ = make_pm(monkeypatch)
    opt = carried_runner(trade)
    opt['last_processed_option_bar_end'] = watermark
    begin = datetime.fromisoformat(start)
    bar = OptionCandle(begin, begin+timedelta(minutes=5), 200, 201, 199, 200, 1, '51333')
    await pm.update_option_market_events([], {'51333': [bar]}, current_time=bar.end)
    assert trade['state'] == 'T1_HIT'
    assert datetime.fromisoformat(opt['last_processed_option_bar_end']) == bar.end


@pytest.mark.asyncio
async def test_unknown_weekday_session_does_not_invent_holiday_outage(monkeypatch):
    pm,trade,sender=make_pm(monkeypatch)
    opt=carried_runner(trade)
    await pm.update_option_market_events([],{},current_time=datetime(2026,10,23,10,2,tzinfo=IST))
    assert not sender.call_args_list
    assert opt["locked_option_sl"] == 196
    assert trade["state"] == "T1_HIT"


@pytest.mark.asyncio
async def test_confirmed_open_session_warns_once_for_option_outage(monkeypatch):
    pm,trade,sender=make_pm(monkeypatch)
    opt=carried_runner(trade)
    row={"timestamp":"2026-10-23T10:00:00+05:30","open":22530,"high":22540,
         "low":22520,"close":22525,"complete":True}
    await pm.update_option_market_events([row],{},current_time=datetime(2026,10,23,10,2,tzinfo=IST))
    await pm.update_option_market_events([row],{},current_time=datetime(2026,10,23,10,3,tzinfo=IST))
    assert [call.args[0]["update_type"] for call in sender.call_args_list] == ["MONITORING_UNAVAILABLE"]
    assert opt["locked_option_sl"] == 196


@pytest.mark.asyncio
async def test_provisional_index_guard_survives_outage_and_recovery_closes_before_restored(monkeypatch):
    pm,trade,sender=make_pm(monkeypatch)
    opt=trade["market_context"]["option_trailing"]
    end=datetime(2026,10,23,10,55,tzinfo=IST)
    trade.update(state="T1_HIT",stop_loss=trade["entry_price"])
    opt.update(phase="POST_T1",highest_milestone=1,booked_fraction=.5,remaining_fraction=.5,
               t1_at=(end-timedelta(minutes=7)).isoformat(),locked_option_sl=196,protection_mode="ATR",
               last_processed_option_bar_end=(end-timedelta(minutes=5)).isoformat())
    pm._option_available_until={}
    await pm.update_trades(22553,event_time=end+timedelta(minutes=1))
    assert opt["observed_index_guard_exit"]["status"] == "AWAITING_OPTION_BACKFILL"
    await pm.update_option_market_events([],{},current_time=end+timedelta(minutes=2))
    assert opt["observed_index_guard_exit"]
    start=datetime(2026,10,23,9,15,tzinfo=IST)
    bars=[OptionCandle(start+timedelta(minutes=5*i),start+timedelta(minutes=5*(i+1)),
                       200,201,199,200,1,"51333") for i in range(20)]
    await pm.update_option_market_events([],{"51333":bars},current_time=end+timedelta(minutes=2))
    assert trade["state"] == "CLOSED"
    assert opt["exit"]["reason"] == "STOPPED_OUT_AT_BE"
    assert datetime.fromisoformat(opt["exit"]["event_at"]) == end+timedelta(minutes=1)
    assert "MONITORING_RESTORED" not in [call.args[0]["update_type"] for call in sender.call_args_list]


@pytest.mark.parametrize("price",["nan","inf","-inf",-1,0,True])
def test_tick_feed_rejects_non_executable_index_values(price):
    from fetchers.tick_feed import TickFeed
    feed=TickFeed()
    feed._on_ticks(None,{"LTP":price})
    assert feed.get_latest_price() is None
    assert feed.get_latest_observation() is None


def test_tick_feed_latest_observation_requires_recent_receipt():
    from datetime import timezone
    from fetchers.tick_feed import TickFeed
    feed=TickFeed()
    feed._on_ticks(None,{"LTP":22525})
    observation=feed.get_latest_observation()
    assert observation[0] == 22525
    assert observation[1].tzinfo == timezone.utc
    feed._latest_received_at=datetime.now(timezone.utc)-timedelta(seconds=6)
    assert feed.get_latest_observation(max_age_seconds=5) is None
    assert feed.get_latest_price() == 22525  # Legacy accessor keeps its price-only contract.


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh",[True,False])
async def test_new_policy_tick_uses_recent_receipt_and_skips_stale_lifecycle(monkeypatch,fresh):
    from datetime import timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import main
    from fetchers.tick_feed import TickFeed
    feed=TickFeed()
    feed._feed=object()
    feed._on_ticks(None,{"LTP":22470})
    if not fresh:
        feed._latest_received_at=datetime.now(timezone.utc)-timedelta(seconds=6)
    receipt=feed._latest_received_at
    trade={"state":"T1_HIT","market_context":{"option_trailing":{
        "policy_version":"option_atr_v1","contract":{"security_id":"51333"}}}}
    positions=SimpleNamespace(active_trades=[trade],update_atr_trailing=AsyncMock(),
        update_option_market_events=AsyncMock(),update_trades=AsyncMock(return_value=[]))
    dispatcher=SimpleNamespace(on_spot=AsyncMock(),on_candles=AsyncMock())
    fetcher=SimpleNamespace(watch_candles=[],fetch_option_candles=AsyncMock(return_value=[]))
    monkeypatch.setattr(main.asyncio,"sleep",AsyncMock())
    monkeypatch.setattr(main.settings,"tick_exit_check_interval_seconds",1)
    await main._sleep_with_tick_exits(1,feed,positions,SimpleNamespace(),dispatcher,price_fetcher=fetcher)
    assert positions.update_trades.await_args.args == (22470,)
    assert positions.update_trades.await_args.kwargs == ({"event_time":receipt} if fresh else {"include_option_policy":False,"allow_t2_milestone":False})


@pytest.mark.asyncio
async def test_tick_sleep_replays_buffered_dhan_ticks_and_attempts_safe_ambiguity_resolution(monkeypatch):
    from datetime import timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import main
    from fetchers.tick_feed import TickFeed

    feed = TickFeed()
    feed._feed = object()
    ltt = datetime.now(timezone.utc).strftime("%H:%M:%S")
    feed._on_ticks(None, {"LTP": 22470, "LTT": ltt})
    feed._on_ticks(None, {"LTP": 22469, "LTT": ltt})
    positions = SimpleNamespace(active_trades=[{"state": "T1_HIT"}],
        update_trades=AsyncMock(return_value=[]), record_reference_spot_quote=Mock(),
        resolve_reference_ambiguities_from_feed=AsyncMock(return_value=0))
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(main.settings, "tick_exit_check_interval_seconds", 1)
    monkeypatch.setattr(main, "_schedule_reference_recovery", lambda *_: None)

    await main._sleep_with_tick_exits(1, feed, positions, SimpleNamespace())

    assert positions.update_trades.await_count == 2
    assert positions.update_trades.await_args_list[0].args == (22470.0,)
    assert positions.update_trades.await_args_list[1].args == (22469.0,)
    assert all("market_event_time" in call.kwargs for call in positions.update_trades.await_args_list)
    assert positions.record_reference_spot_quote.call_count == 2
    positions.resolve_reference_ambiguities_from_feed.assert_awaited_once_with(feed)


@pytest.mark.asyncio
async def test_terminal_pending_delivery_retries_on_closed_session_startup(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock,MagicMock
    import main
    pm,trade,sender=make_pm(monkeypatch)
    await pm.update_trades(22525)
    sender.return_value="FAILED"
    await pm.update_trades(22553)
    assert trade["state"] == "CLOSED"
    pm.active_trades=[]
    pm._option_alert_trades=[trade]
    sender.reset_mock()
    sender.return_value="DELIVERED"
    price=SimpleNamespace(fetch_previous_day_ohlc=AsyncMock(return_value=(23100,22900)),
                          reset_vwap=lambda:None, fetch_latest_candle=AsyncMock())
    for name,instance in {"PositionManager":pm,"PriceFetcher":price,"AresEngine":MagicMock(),
        "OIFetcher":MagicMock(),"LevelFetcher":MagicMock(),"Storage":MagicMock(),
        "SignalDispatcher":MagicMock(),"OrderManager":MagicMock(),"MLCollector":MagicMock(),
        "TickFeed":MagicMock(),"SignalPredictor":MagicMock(),"PredictionLogger":MagicMock()}.items():
        monkeypatch.setattr(main,name,MagicMock(return_value=instance))
    monkeypatch.setattr(main,"load_dhan_credentials_from_supabase",lambda:None)
    monkeypatch.setattr(main,"is_expiry_day_from_api",AsyncMock(return_value=False))
    monkeypatch.setattr(main,"send_startup_alert",AsyncMock())
    monkeypatch.setattr(main,"initialize_oi_watch_outbox",AsyncMock(return_value=None))
    monkeypatch.setattr(main,"print_banner",lambda *a,**kw:None)
    clock=MagicMock()
    clock.now.return_value=datetime(2026,10,23,15,30)
    monkeypatch.setattr(main,"datetime",clock)
    await main.run()
    assert sender.await_count == 1
    assert sender.await_args.args[0]["update_type"] == "STOPPED_OUT_AT_BE"
    price.fetch_latest_candle.assert_not_awaited()
