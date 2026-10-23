"""Public lifecycle recovery across real revision conflicts and process restarts."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from options_candles import OptionCandle
from tests.unit.test_option_lifecycle_commit_boundary import IST, Ledger, make_pm


class ConflictLedger(Ledger):
    """Inject a committed competing state; enforce the transaction's invariants."""
    def __init__(self, context, state='T1_HIT', revision=4):
        super().__init__()
        self.revision = revision
        context = {**deepcopy(context), 'revision': revision, 'alert_events': {}}
        self.rows = [{'active_state': state, 'score': context['highest_milestone'],
                      'option_trailing': context}]
        self.conflicts = 0

    def apply_option_lifecycle_transition(self, trade_id, revision, event_id, payload):
        if self.fail:
            raise ConnectionError('database unavailable')
        if event_id not in self.events and revision != self.revision:
            self.conflicts += 1
            raise ValueError(f'Revision conflict: expected {revision}, found {self.revision}')
        previous = self.rows[-1]['option_trailing']
        candidate = payload['option_trailing']
        assert candidate['highest_milestone'] >= previous['highest_milestone']
        assert candidate['locked_option_sl'] >= previous['locked_option_sl']
        return super().apply_option_lifecycle_transition(trade_id, revision, event_id, payload)

    def get_option_lifecycle_state(self, trade_id):
        row = self.rows[-1]
        return {'option_trailing': deepcopy(row['option_trailing']),
                'active_state': row['active_state'], 'stop_loss': 22552.1, 'result_state': 'OPEN'}


def post_t1(trade, stop=127):
    trade.update(state='T1_HIT', stop_loss=trade['entry_price'])
    option = trade['market_context']['option_trailing']
    option.update(phase='POST_T1', highest_milestone=1, booked_fraction=.5,
                  remaining_fraction=.5, t1_at='2026-10-23T10:09:00+05:30',
                  locked_option_sl=stop, protection_mode='ATR',
                  last_processed_option_bar_end='2026-10-23T10:50:00+05:30')
    return option


def exit_bar(close):
    end = datetime(2026, 10, 23, 10, 55, tzinfo=IST)
    return OptionCandle(end-timedelta(minutes=5), end, 200, 201, close-1, close, 1, '51333')


@pytest.mark.asyncio
async def test_terminal_observation_survives_conflict_with_higher_committed_stop(monkeypatch):
    pm, trade, _ = make_pm(monkeypatch)
    option = post_t1(trade)
    pm.analytics = ConflictLedger({**option, 'locked_option_sl': 150})
    bar = exit_bar(120)
    await pm.update_option_atr_trailing({'51333': [bar]}, current_time=bar.end)
    assert pm.analytics.conflicts == 1
    assert trade['state'] == 'CLOSED'
    assert pm.analytics.rows[-1]['active_state'] == 'CLOSED'
    assert pm.analytics.rows[-1]['option_trailing']['locked_option_sl'] == 150
    assert pm.analytics.rows[-1]['score'] == 1
    assert pm._option_pending[trade['id']] == []


@pytest.mark.asyncio
async def test_queued_t2_then_exit_keeps_observed_score_and_higher_stop_on_conflict(monkeypatch):
    pm, trade, _ = make_pm(monkeypatch)
    option = post_t1(trade, stop=175)
    pm.analytics = ConflictLedger({**option, 'locked_option_sl': 150})
    pm.analytics.fail = True
    await pm.update_trades(22470, event_time=datetime(2026,10,23,10,51,tzinfo=IST))
    bar = exit_bar(170)
    await pm.update_option_atr_trailing({'51333': [bar]}, current_time=bar.end)
    assert trade['state'] == 'CLOSED'
    assert len(pm._option_pending[trade['id']]) == 2
    pm.analytics.fail = False
    await pm.retry_option_lifecycle_writes()
    committed = pm.analytics.rows[1:]
    assert [row['active_state'] for row in committed] == ['T2_REACHED', 'CLOSED']
    assert [row['score'] for row in committed] == [2, 2]
    assert all(row['option_trailing']['locked_option_sl'] == 175 for row in committed)
    assert option['highest_milestone'] == 2
    assert option['booked_fraction'] == .8
    assert pm._option_pending[trade['id']] == []


@pytest.mark.asyncio
async def test_superseded_t1_dropped_while_newer_protective_raise_is_kept(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    option = trade['market_context']['option_trailing']
    committed = {**option, 'phase':'POST_T1', 'highest_milestone':1, 'remaining_fraction':.5,
                 'booked_fraction':.5, 'locked_option_sl':150, 'protection_mode':'ATR'}
    pm.analytics = ConflictLedger(committed)
    pm.analytics.fail = True
    t1 = datetime(2026,10,23,10,9,tzinfo=IST)
    await pm.update_trades(22525, event_time=t1)
    start = datetime(2026,10,23,9,15,tzinfo=IST)
    bars = [OptionCandle(start+timedelta(minutes=5*i), start+timedelta(minutes=5*(i+1)),
                        200,201,199,200,1,'51333') for i in range(20)]
    await pm.update_option_atr_trailing({'51333':bars},current_time=bars[-1].end)
    pm.analytics.fail = False
    sender.reset_mock()
    await pm.retry_option_lifecycle_writes()
    assert pm.analytics.conflicts == 1
    assert all(row.get('update_type') != 'T1_HIT' for row in pm.analytics.rows[1:])
    assert pm.analytics.rows[-1]['option_trailing']['locked_option_sl'] == 196
    assert trade['state'] == 'T1_HIT'
    assert option['locked_option_sl'] == 196
    assert pm._option_pending[trade['id']] == []


@pytest.mark.asyncio
async def test_delivery_claim_and_final_ack_share_execution_context_lock(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    ack = pm.analytics.acknowledge_option_lifecycle_alert
    statuses = []
    def locked_ack(trade_id, event_id, status, detail=None):
        assert pm._context_locks[trade_id].locked(), 'execution JSON writer must not race acknowledgements'
        statuses.append(status)
        return ack(trade_id, event_id, status, detail)
    pm.analytics.acknowledge_option_lifecycle_alert = locked_ack
    await pm.update_trades(22525)
    assert statuses == ['SENDING', 'DELIVERED']
    assert sender.await_count == 1


@pytest.mark.asyncio
async def test_restarted_manager_never_resubmits_durable_inflight_alert(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    acknowledge = pm.analytics.acknowledge_option_lifecycle_alert
    def lost_ack(trade_id, event_id, status, detail=None):
        if status == 'DELIVERED':
            raise ConnectionError('process stopped before final acknowledgement')
        return acknowledge(trade_id, event_id, status, detail)
    pm.analytics.acknowledge_option_lifecycle_alert = lost_ack
    await pm.update_trades(22525)
    assert sender.await_count == 1
    durable = deepcopy(pm.analytics.rows[-1]['option_trailing'])
    assert next(iter(durable['alert_events'].values()))['status'] == 'SENDING'
    restarted, restored_trade, new_sender = make_pm(monkeypatch)
    restored_trade.update(state='T1_HIT',stop_loss=restored_trade['entry_price'])
    restored_trade['market_context']['option_trailing'] = durable
    restarted.analytics = pm.analytics
    await restarted.retry_option_lifecycle_writes()
    assert new_sender.await_count == 0
    assert next(iter(durable['alert_events'].values()))['status'] == 'SENDING'


@pytest.mark.asyncio
async def test_new_stop_raise_survives_conflict_with_committed_later_milestone(monkeypatch):
    pm, trade, _ = make_pm(monkeypatch)
    option = trade['market_context']['option_trailing']
    committed = {**option, 'phase':'POST_T2', 'highest_milestone':2, 'remaining_fraction':.2,
                 'booked_fraction':.8, 'locked_option_sl':150, 'protection_mode':'ATR',
                 'index_guard_enabled':False}
    pm.analytics = ConflictLedger(committed, state='T2_REACHED')
    pm.analytics.fail = True
    await pm.update_trades(22525, event_time=datetime(2026,10,23,10,9,tzinfo=IST))
    start = datetime(2026,10,23,9,15,tzinfo=IST)
    bars = [OptionCandle(start+timedelta(minutes=5*i),start+timedelta(minutes=5*(i+1)),
                        200,201,199,200,1,'51333') for i in range(20)]
    await pm.update_option_atr_trailing({'51333':bars},current_time=bars[-1].end)
    assert option['locked_option_sl'] == 196
    pm.analytics.fail = False
    await pm.retry_option_lifecycle_writes()
    assert trade['state'] == 'T2_REACHED'
    assert option['highest_milestone'] == 2
    assert option['locked_option_sl'] == 196
    assert pm.analytics.rows[-1]['option_trailing']['locked_option_sl'] == 196


@pytest.mark.parametrize('delivery_status,expected_sends', [('PENDING', 1), ('SENDING', 0)])
@pytest.mark.asyncio
async def test_database_startup_recovers_terminal_outbox_without_reopening_trade(monkeypatch, delivery_status, expected_sends):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import position_manager as module
    from position_manager import PositionManager

    _, closed_trade, _ = make_pm(monkeypatch)
    option = post_t1(closed_trade)
    option.update(phase='CLOSED', remaining_fraction=0, monitoring_status='CLOSED')
    closed_trade.update(state='CLOSED', exit_type='ATR_SL_HIT', exit_price=None,
                        exit_timestamp='2026-10-23T05:25:00Z')
    snapshot = {'update_type':'ATR_SL_HIT', 'event_time':closed_trade['exit_timestamp'],
                'trade':deepcopy(closed_trade), 'option_trailing':deepcopy(option)}
    option['alert_events'] = {'exit-event': {'status':delivery_status, 'snapshot':snapshot}}
    store = {
        'active_trades':[deepcopy(closed_trade)],
        'trade_analytics':[{'id':closed_trade['id'], 'result_state':'ATR_SL_HIT',
                            'market_context':deepcopy(closed_trade['market_context'])}],
        'ml_collection':[],
    }
    class Query:
        def __init__(self, name):
            self.name, self.ids = name, None
        def select(self, *args, **kwargs): return self
        def order(self, *args, **kwargs): return self
        def range(self, *args, **kwargs): return self
        def in_(self, column, ids):
            self.ids = ids
            return self
        def execute(self):
            rows = deepcopy(store[self.name])
            return SimpleNamespace(data=[row for row in rows if self.ids is None or row['id'] in self.ids])
    database = SimpleNamespace(table=lambda name:Query(name))
    ledger = Ledger()
    ledger.rows = [{'option_trailing':deepcopy(option)}]
    monkeypatch.setattr(module, 'create_client', lambda *args, **kwargs:database)
    monkeypatch.setattr(module, 'AnalyticsLogger', lambda:ledger)
    sender = AsyncMock(return_value='DELIVERED')
    monkeypatch.setattr(module, 'send_option_lifecycle_event', sender)
    restored = PositionManager()
    assert restored.is_initialized is True
    assert restored.active_trades == []
    await restored.retry_option_lifecycle_writes()
    await restored.retry_option_lifecycle_writes()
    assert sender.await_count == expected_sends
    assert all(trade['state'] == 'CLOSED' for trade in restored._option_alert_trades)


@pytest.mark.asyncio
async def test_superseded_t1_with_stronger_seed_recovers_as_raise_not_t1_alert(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    option = trade['market_context']['option_trailing']
    committed = {**option, 'phase':'POST_T2', 'highest_milestone':2, 'remaining_fraction':.2,
                 'booked_fraction':.8, 'locked_option_sl':150, 'protection_mode':'ATR',
                 'index_guard_enabled':False}
    pm.analytics = ConflictLedger(committed, state='T2_REACHED')
    pm.analytics.fail = True
    start = datetime(2026,10,23,9,15,tzinfo=IST)
    bars = [OptionCandle(start+timedelta(minutes=5*i),start+timedelta(minutes=5*(i+1)),
                        200,201,199,200,1,'51333') for i in range(20)]
    await pm.update_option_atr_trailing({'51333':bars}, current_time=bars[-1].end)
    await pm.update_trades(22525, event_time=bars[-1].end+timedelta(minutes=1))
    assert option['locked_option_sl'] == 196
    pm.analytics.fail = False
    sender.reset_mock()
    await pm.retry_option_lifecycle_writes()
    assert option['phase'] == 'POST_T2'
    assert option['locked_option_sl'] == 196
    assert not [call for call in sender.call_args_list if call.args[0]['update_type'] == 'T1_HIT']
    assert [call.args[0]['update_type'] for call in sender.call_args_list] == ['OPTION_SL_RATCHETED']
