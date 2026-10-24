"""T2 guard consumption survives the database's JSON merge and a restart."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from options_candles import OptionCandle
from tests.unit.test_option_lifecycle_commit_boundary import IST, Ledger, make_pm
from tests.unit.test_option_conflict_recovery import ConflictLedger, post_t1


class JsonMergeLedger(Ledger):
    """Preserve omitted option keys like the lifecycle RPC's jsonb concatenation."""
    def __init__(self):
        super().__init__()
        self.submitted = []

    def apply_option_lifecycle_transition(self, trade_id, revision, event_id, payload):
        self.submitted.append(deepcopy(payload))
        previous = self.rows[-1]['option_trailing'] if self.rows else {}
        merged = {**deepcopy(payload), 'option_trailing': {
            **deepcopy(previous), **deepcopy(payload['option_trailing'])}}
        return super().apply_option_lifecycle_transition(trade_id, revision, event_id, merged)


@pytest.mark.asyncio
async def test_recovered_t2_clears_guard_durably_and_restart_delivers_runner_raise(monkeypatch):
    pm, trade, _ = make_pm(monkeypatch)
    option = post_t1(trade, stop=127)
    pm.analytics = JsonMergeLedger()
    pm._option_available_until = {}
    touch_at = datetime(2026, 10, 23, 10, 56, tzinfo=IST)
    await pm.update_trades(22470, candle_high=22554, candle_low=22470, event_time=touch_at)
    assert option['observed_index_guard_exit']['status'] == 'AWAITING_OPTION_BACKFILL'
    assert pm.analytics.rows[-1]['option_trailing']['observed_index_guard_exit']
    start = datetime(2026, 10, 23, 9, 15, tzinfo=IST)
    bars = [OptionCandle(start+timedelta(minutes=5*i), start+timedelta(minutes=5*(i+1)),
                        200, 201, 199, 200, 1, '51333') for i in range(20)]
    await pm.update_option_market_events([], {'51333': bars}, current_time=touch_at)
    t2 = next(payload for payload in pm.analytics.submitted if payload['update_type'] == 'T2_HIT')
    assert 'observed_index_guard_exit' in t2['option_trailing']
    assert t2['option_trailing']['observed_index_guard_exit'] is None
    durable = deepcopy(pm.analytics.rows[-1]['option_trailing'])
    assert durable['observed_index_guard_exit'] is None
    assert trade['state'] == 'T2_REACHED'
    assert durable['index_guard_enabled'] is False
    assert durable['remaining_fraction'] == .2

    restored, restored_trade, sender = make_pm(monkeypatch)
    restored_trade.update(state='T2_REACHED', stop_loss=restored_trade['entry_price'])
    restored_trade['market_context']['option_trailing'] = durable
    restored.analytics = pm.analytics
    end = bars[-1].end+timedelta(minutes=5)
    bars.append(OptionCandle(end-timedelta(minutes=5), end, 210, 211, 209, 210, 1, '51333'))
    await restored.update_option_atr_trailing({'51333': bars}, current_time=end)
    raises = [call.args[0] for call in sender.call_args_list if call.args[0]['update_type'] == 'OPTION_SL_RATCHETED']
    assert len(raises) == 1
    assert raises[0]['option_trailing']['phase'] == 'POST_T2'
    assert raises[0]['option_trailing']['remaining_fraction'] == .2
    assert durable['locked_option_sl'] > 196


@pytest.mark.asyncio
async def test_same_observation_t1_t2_also_emits_explicit_guard_clear(monkeypatch):
    pm, trade, _ = make_pm(monkeypatch)
    pm.analytics = JsonMergeLedger()
    await pm.update_trades(22470, event_time=datetime(2026, 10, 23, 10, 16, tzinfo=IST))
    assert [payload['update_type'] for payload in pm.analytics.submitted] == ['T1_HIT', 'T2_HIT']
    t2 = pm.analytics.submitted[-1]
    assert 'observed_index_guard_exit' in t2['option_trailing']
    assert t2['option_trailing']['observed_index_guard_exit'] is None
    assert trade['state'] == 'T2_REACHED'
    assert pm.analytics.rows[-1]['score'] == 2


@pytest.mark.asyncio
async def test_rebasing_older_raise_onto_committed_t2_keeps_guard_consumed(monkeypatch):
    pm, trade, sender = make_pm(monkeypatch)
    option = post_t1(trade, stop=127)
    pm.analytics = ConflictLedger({**option, 'phase': 'POST_T2', 'highest_milestone': 2,
        'booked_fraction': .8, 'remaining_fraction': .2, 'locked_option_sl': 150,
        'index_guard_enabled': False, 'pending_index_observations': [],
        'observed_index_guard_exit': None}, state='T2_REACHED')
    pm.analytics.fail = True
    pm._option_available_until = {}
    observed_at = datetime(2026, 10, 23, 11, 1, tzinfo=IST)
    await pm.update_trades(22553, event_time=observed_at)
    start = datetime(2026, 10, 23, 9, 15, tzinfo=IST)
    bars = [OptionCandle(start+timedelta(minutes=5*i), start+timedelta(minutes=5*(i+1)),
                        200, 201, 199, 200, 1, '51333') for i in range(20)]
    await pm.update_option_market_events([], {'51333': bars}, current_time=observed_at)
    pm.analytics.fail = False
    sender.reset_mock()
    await pm.retry_option_lifecycle_writes()
    assert trade['state'] == 'T2_REACHED'
    assert option['observed_index_guard_exit'] is None
    assert option['pending_index_observations'] == []
    assert pm.analytics.rows[-1]['option_trailing']['observed_index_guard_exit'] is None
    raises = [call.args[0] for call in sender.call_args_list if call.args[0]['update_type'] == 'OPTION_SL_RATCHETED']
    assert len(raises) == 1
    assert raises[0]['option_trailing']['observed_index_guard_exit'] is None
    assert raises[0]['option_trailing']['remaining_fraction'] == .2


@pytest.mark.parametrize('exit_kind', ['ATR_SL_HIT', 'STOPPED_OUT_AT_BE'])
@pytest.mark.asyncio
async def test_earlier_queued_raise_cannot_reopen_observed_terminal_trade(monkeypatch, exit_kind):
    pm, trade, sender = make_pm(monkeypatch)
    option = post_t1(trade, stop=127)
    pm.analytics = ConflictLedger({**option, 'phase': 'POST_T2', 'highest_milestone': 2,
        'booked_fraction': .8, 'remaining_fraction': .2, 'locked_option_sl': 150,
        'index_guard_enabled': False, 'pending_index_observations': [],
        'observed_index_guard_exit': None}, state='T2_REACHED')
    pm.analytics.fail = True
    start = datetime(2026, 10, 23, 9, 15, tzinfo=IST)
    bars = [OptionCandle(start+timedelta(minutes=5*i), start+timedelta(minutes=5*(i+1)),
                        200, 201, 199, 200, 1, '51333') for i in range(20)]
    await pm.update_option_atr_trailing({'51333': bars}, current_time=bars[-1].end)
    if exit_kind == 'ATR_SL_HIT':
        end = bars[-1].end+timedelta(minutes=5)
        bars.append(OptionCandle(end-timedelta(minutes=5), end, 200, 201, 179, 180, 1, '51333'))
        await pm.update_option_atr_trailing({'51333': bars}, current_time=end)
    else:
        await pm.update_trades(22553, event_time=bars[-1].end+timedelta(minutes=1))
    assert trade['state'] == 'CLOSED'
    pm.analytics.fail = False
    sender.reset_mock()
    await pm.retry_option_lifecycle_writes()
    assert trade['state'] == 'CLOSED'
    assert option['phase'] == 'CLOSED'
    assert option['remaining_fraction'] == 0
    if exit_kind == 'ATR_SL_HIT':
        committed = pm.analytics.rows[-1]
        assert committed['active_state'] == 'CLOSED'
        assert committed['score'] == option['highest_milestone'] == 2
        assert committed['option_trailing']['booked_fraction'] == .8
        assert committed['option_trailing']['exit']['exited_fraction'] == .2
        assert option['exit']['exited_fraction'] == .2
        exits = [call.args[0] for call in sender.call_args_list if call.args[0]['update_type'] == 'ATR_SL_HIT']
        assert len(exits) == 1
        assert exits[0]['option_trailing']['exit']['exited_fraction'] == .2
    else:
        # An observed index-BE exit conflicting with committed T2 needs repair;
        # it must stay observed closed while that reconciliation is pending.
        assert pm.analytics.rows[-1]['active_state'] == 'T2_REACHED'
        assert pm._option_pending[trade['id']]
