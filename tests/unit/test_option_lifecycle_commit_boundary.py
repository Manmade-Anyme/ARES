"""Regression cases at the observed-event / transactional ledger boundary."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import position_manager as module
from position_manager import PositionManager
from models import OptionTrailingContext, OptionTrailingContract
from options_candles import OptionCandle

IST = timezone(timedelta(hours=5, minutes=30))

class Ledger:
    def __init__(self):
        self.revision = 1
        self.events = {}
        self.rows = []
        self.fail = False
    def apply_option_lifecycle_transition(self, trade_id, revision, event_id, payload):
        if self.fail:
            raise ConnectionError('database unavailable')
        if event_id in self.events:
            return deepcopy(self.events[event_id])
        assert revision == self.revision
        self.revision += 1
        context = deepcopy(payload['option_trailing'])
        context['revision'] = self.revision
        context['alert_events'] = deepcopy(self.rows[-1]['option_trailing'].get('alert_events', {})) if self.rows else {}
        if payload.get('alert_snapshot'):
            context['alert_events'][event_id] = {'status': 'PENDING', 'snapshot': deepcopy(payload['alert_snapshot'])}
        payload = deepcopy(payload)
        payload['option_trailing'] = context
        self.rows.append(payload)
        result = {'success': True, 'revision': self.revision, 'option_trailing': context}
        self.events[event_id] = deepcopy(result)
        return result
    def acknowledge_option_lifecycle_alert(self, trade_id, event_id, status, detail=None):
        self.rows[-1]['option_trailing']['alert_events'][event_id]['status'] = status
        return {'success': True, 'claimed': status == 'SENDING'}


def make_pm(monkeypatch):
    pm = object.__new__(PositionManager)
    pm.is_initialized = True
    pm._trade_writes = {}
    pm._context_locks = {}
    pm.analytics = Ledger()
    opt = OptionTrailingContext(contract=OptionTrailingContract('51333', 'NSE_FNO', '2026-10-27', 22550, 'PE'),
        original_option_sl=123.65, original_option_entry=139.25).to_dict()
    trade = dict(id='trade-1950', display_id='1950', direction='BEARISH', state='OPEN', entry_price=22552.1,
        stop_loss=22577.1, target_1=22527.1, target_2=22472.1, entry_timestamp='2026-10-23T04:38:38+00:00',
        market_context={'option_trailing': opt})
    pm.active_trades = [trade]
    pm._option_available_until = {trade['id']: datetime(2099,1,1,tzinfo=IST)}
    sender = AsyncMock(return_value='DELIVERED')
    monkeypatch.setattr(module, 'send_option_lifecycle_event', sender, raising=False)
    return pm, trade, sender

@pytest.mark.asyncio
async def test_committed_revision_advances_before_next_milestone(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    await pm.update_trades(22525, candle_timestamp=datetime(2026,10,23,10,9,tzinfo=IST))
    assert trade['market_context']['option_trailing']['revision'] == 2
    assert pm.analytics.rows[-1]['score'] == 1
    assert sender.call_count == 1
    await pm.update_trades(22470, candle_timestamp=datetime(2026,10,23,10,15,tzinfo=IST))
    assert trade['market_context']['option_trailing']['revision'] == 3
    assert pm.analytics.rows[-1]['score'] == 2
    assert len(pm.analytics.events) == 2

@pytest.mark.asyncio
async def test_failed_commit_retains_protection_and_sends_no_recorded_card(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    pm.analytics.fail = True
    await pm.update_trades(22470, candle_timestamp=datetime(2026,10,23,10,15,tzinfo=IST))
    assert trade['state'] == 'T2_REACHED'
    assert not [c for c in sender.call_args_list if c.args[0]['update_type'] in ('T1_HIT','T2_HIT')]
    pm.analytics.fail = False
    await pm.retry_option_lifecycle_writes()
    assert pm.analytics.rows[-1]['score'] == 2
    assert trade['market_context']['option_trailing']['revision'] == 3

@pytest.mark.asyncio
async def test_index_be_retains_target_booking_and_weighted_index_points(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    await pm.update_trades(22525)
    await pm.update_trades(22553)
    opt = trade['market_context']['option_trailing']
    assert opt['booked_fraction'] == .5
    assert opt['remaining_fraction'] == 0
    assert opt['exit']['exited_fraction'] == .5
    assert pm.analytics.rows[-1]['pnl_points'] == pytest.approx(12.5)

@pytest.mark.asyncio
async def test_ambiguous_delivery_is_not_retried(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    sender.return_value = 'AMBIGUOUS'
    await pm.update_trades(22525)
    await pm.retry_option_lifecycle_writes()
    assert sender.call_count == 1

@pytest.mark.asyncio
async def test_t1_seeds_confirmed_history_and_first_close_can_exit(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    start = datetime(2026,10,23,9,15,tzinfo=IST)
    history = [OptionCandle(start+timedelta(minutes=5*i), start+timedelta(minutes=5*(i+1)), 200,201,199,200,1,'51333') for i in range(20)]
    t1 = start+timedelta(minutes=102)
    await pm.update_option_atr_trailing({'51333': history}, current_time=t1)
    await pm.update_trades(22525,candle_timestamp=t1)
    assert trade['market_context']['option_trailing']['locked_option_sl'] > 190
    close = OptionCandle(start+timedelta(minutes=100),start+timedelta(minutes=105),200,201,180,190,1,'51333')
    await pm.update_option_atr_trailing({'51333': history+[close]},current_time=close.end)
    assert trade['state'] == 'CLOSED'
    assert pm.analytics.rows[-1]['exit_price'] is None
    assert pm.analytics.rows[-1]['pnl_points'] is None
    assert trade['market_context']['option_trailing']['exit']['option_close'] == 190

@pytest.mark.asyncio
async def test_equal_time_t1_uses_previous_stop_before_current_bar_raise(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    start = datetime(2026,10,23,9,15,tzinfo=IST)
    bars = [OptionCandle(start+timedelta(minutes=5*i), start+timedelta(minutes=5*(i+1)),200,201,199,200,1,'51333') for i in range(20)]
    end = start+timedelta(minutes=105)
    bars.append(OptionCandle(end-timedelta(minutes=5),end,200,201,180,190,1,'51333'))
    index = [{'timestamp': (end-timedelta(minutes=1)).isoformat(), 'open':22540,'high':22545,'low':22520,'close':22525,'complete':True}]
    await pm.update_option_market_events(index, {'51333':bars},current_time=end)
    assert trade['state'] == 'CLOSED'
    assert trade['market_context']['option_trailing']['highest_milestone'] == 1
    assert trade['market_context']['option_trailing']['exit']['option_stop'] == 196
    assert pm.analytics.rows[-1]['exit_price'] == 22525
    assert pm.analytics.rows[-1]['pnl_points'] == pytest.approx(.5*25 + .5*(22552.1-22525))

@pytest.mark.asyncio
async def test_failed_delivery_retries_with_same_committed_snapshot(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    sender.side_effect = ['FAILED','DELIVERED']
    await pm.update_trades(22525)
    assert sender.await_count == 1
    original = deepcopy(sender.call_args.args[0])
    await pm.retry_option_lifecycle_writes()
    assert sender.await_count == 2
    assert sender.call_args.args[0] == original
    assert pm.analytics.revision == 2

@pytest.mark.asyncio
async def test_lost_ack_after_send_does_not_submit_again(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    real_ack = pm.analytics.acknowledge_option_lifecycle_alert
    def ack(trade_id,event_id,status,detail=None):
        if status == 'DELIVERED':
            raise ConnectionError('ack response lost')
        return real_ack(trade_id,event_id,status,detail)
    pm.analytics.acknowledge_option_lifecycle_alert = ack
    await pm.update_trades(22525)
    await pm.retry_option_lifecycle_writes()
    assert sender.await_count == 1
    assert next(iter(pm.analytics.rows[-1]['option_trailing']['alert_events'].values()))['status'] == 'SENDING'

@pytest.mark.asyncio
async def test_missed_option_close_is_processed_before_later_queued_t2(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    end = datetime(2026,10,23,10,55,tzinfo=IST)
    opt = trade['market_context']['option_trailing']
    trade.update(state='T1_HIT',stop_loss=trade['entry_price'])
    opt.update(phase='POST_T1',highest_milestone=1,booked_fraction=.5,remaining_fraction=.5,
               t1_at=(end-timedelta(minutes=7)).isoformat(),protection_mode='ATR',locked_option_sl=196,
               last_processed_option_bar_end=(end-timedelta(minutes=5)).isoformat())
    pm._option_available_until = {}
    await pm.update_trades(22470,candle_timestamp=end+timedelta(minutes=1))
    assert trade['state'] == 'T1_HIT'
    assert opt['pending_index_observations']
    start = datetime(2026,10,23,9,15,tzinfo=IST)
    history = [OptionCandle(start+timedelta(minutes=5*i),start+timedelta(minutes=5*(i+1)),200,201,199,200,1,'51333') for i in range(19)]
    history.append(OptionCandle(end-timedelta(minutes=5),end,200,201,180,190,1,'51333'))
    await pm.update_option_market_events([],{'51333':history},current_time=end+timedelta(minutes=2))
    assert trade['state'] == 'CLOSED'
    assert opt['highest_milestone'] == 1
    assert not [row for row in pm.analytics.rows if row['score'] == 2]
    assert opt['exit']['event_at'] == end.isoformat()

@pytest.mark.parametrize('missing', ['security_id','expiry_date','strike','option_type','entry_limit','option_sl'])
def test_pinned_contract_never_fabricates_missing_metadata(missing):
    sizing = {'security_id':'51333','expiry_date':'2026-10-27','strike':22550,'option_type':'PE','entry_limit':139.25,'option_sl':123.65}
    sizing.pop(missing)
    assert PositionManager._validated_option_contract(sizing) is None

def test_pinned_contract_uses_expiry_date_and_valid_identity():
    contract = PositionManager._validated_option_contract({'security_id':'51333','expiry_date':'2026-10-27','strike':22550,'option_type':'PE','entry_limit':139.25,'option_sl':123.65})
    assert contract.expiry == '2026-10-27'
    assert contract.security_id == '51333'

@pytest.mark.asyncio
async def test_unkeyed_anonymous_option_history_cannot_raise_or_exit(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    opt = trade['market_context']['option_trailing']
    trade['state'] = 'T1_HIT'
    opt.update(phase='POST_T1', t1_at='2026-10-23T10:09:00+05:30', highest_milestone=1,
               remaining_fraction=.5,booked_fraction=.5,locked_option_sl=152,protection_mode='ATR')
    end = datetime(2026,10,23,10,55,tzinfo=IST)
    anonymous = OptionCandle(end-timedelta(minutes=5),end,140,145,130,135,1,None)
    await pm.update_option_atr_trailing([anonymous],current_time=end)
    assert trade['state'] == 'T1_HIT'
    assert opt['locked_option_sl'] == 152
    assert pm.analytics.rows == []
