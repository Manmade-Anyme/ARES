"""Behavioral latency regressions: injected delays at database/provider boundaries."""
from types import SimpleNamespace
from unittest.mock import MagicMock
from contextlib import nullcontext
from datetime import datetime, timezone
import runpy
import pytest

import system_one.consumer as consumer


def test_snapshot_query_is_measured_separately_from_signal_lookup(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock[0])
    db = MagicMock()
    signal = {"signal_uuid": "sig", "direction": "BULLISH", "spot_at_signal": 100,
              "target_1": 110, "target_2": 120, "stop_loss": 90, "setup_type": "BREAKOUT"}
    snapshot = {"signal_uuid": "sig", "snapshot_uuid": "snap", "timestamp": "2026-10-02T04:30:00Z"}
    def table(name):
        query = MagicMock()
        for method in ("select", "eq", "in_", "gte", "order", "limit", "is_", "range"):
            getattr(query, method).return_value = query
        query.not_ = query
        def execute():
            if name == "ml_collection":
                clock[0] += 2
                return SimpleNamespace(data=[snapshot])
            if name == "ares_signals":
                clock[0] += 3
                return SimpleNamespace(data=[signal])
            if name == "llm_predictions":
                return SimpleNamespace(data=[published])
            return SimpleNamespace(data=[])
        query.execute.side_effect = execute
        return query
    db.table.side_effect = table
    job = {"id": 1, "owner_token": "owner", "invocation_token": "invoke",
           "invocation_started_at": "2026-10-02T04:30:03Z",
           "invocation_dispatch_deadline_at": "2026-10-02T04:31:00Z"}
    published = {}
    def rpc(name, params):
        query = MagicMock()
        def execute():
            if name == "poll_jev_alert_jobs":
                return SimpleNamespace(data=[{"id": 1, "signal_uuid": "sig", "prediction_id": 9}])
            if name == "poll_jev_signals":
                clock[0] += 2
                return SimpleNamespace(data=[snapshot])
            if name == "read_jev_signal":
                clock[0] += 3
                return SimpleNamespace(data=[signal])
            if name == "complete_llm_prediction_job":
                published.update(params["p_prediction"])
                clock[0] += 7
                return SimpleNamespace(data=[{"id": 9}])
            return SimpleNamespace(data=[job])
        query.execute.side_effect = execute
        return query
    db.rpc.side_effect = rpc
    result = SimpleNamespace(t1_hit_prob=.6, t2_hit_prob=.3, sl_hit_prob=.2,
        regime="trending", regime_distribution={}, regime_confidence=.9, setup_quality=7,
        is_trap_prob=.1, engine_name="jev", raw_response={}, latency_ms=5000)
    def invoke(*args, **kwargs):
        clock[0] += 5
        return result
    def deliver(*args, **kwargs):
        clock[0] += 11
        return "SENT"
    monkeypatch.setattr(consumer, "invoke_jev", invoke)
    monkeypatch.setattr(consumer, "send_jev_followup", deliver)
    polled = consumer._poll_eligible_signals(db)
    assert consumer.process_signal(db, polled[0]) == "COMPLETED"
    consumer._recover_pending_alerts(db)
    assert published["latency_snapshot_read_ms"] == 2000
    assert published["latency_signal_read_ms"] == 3000
    # End-to-end is finalized after both slow persistence and slow delivery,
    # with the database deriving event age. It is not the five-second API time.
    final = [call for call in db.rpc.call_args_list if call.args[0] == "record_llm_prediction_latency"]
    assert len(final) == 2
    assert final[0].args[1]["p_persistence_ms"] == 7000
    assert final[0].args[1]["p_delivery_ms"] is None
    assert final[0].args[1]["p_confirmed_sent"] is False
    assert final[1].args[1]["p_persistence_ms"] is None  # SQL retains the first observation.
    assert final[1].args[1]["p_delivery_ms"] == 11000
    assert final[1].args[1]["p_confirmed_sent"] is True
    assert published.get("latency_total_ms") is None


# Small external-boundary fixture shared by failure/recovery cases. All consumer
# logic runs normally; only database and hosted inference/delivery are simulated.
@pytest.fixture
def flow(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(consumer.time, 'monotonic', lambda: clock[0])
    state = SimpleNamespace(
        clock=clock, published={}, recorded=[], table_errors={}, rpc_errors={}, updates=[],
        rpc_data={}, as_dict=False, rows={}, costs={}, alert='SENT')
    state.signal = {'signal_uuid': 'sig', 'direction': 'BULLISH', 'spot_at_signal': 100,
        'target_1': 110, 'target_2': 120, 'stop_loss': 90, 'setup_type': 'TREND_CONTINUATION'}
    state.snapshot = {'signal_uuid': 'sig', 'snapshot_uuid': 'snap', 'timestamp': '2026-10-02T04:30:00Z'}
    state.job = {'id': 1, 'owner_token': 'owner', 'invocation_token': 'invoke',
        'invocation_started_at': '2026-10-02T04:30:03Z',
        'invocation_dispatch_deadline_at': '2026-10-02T04:31:00Z'}
    state.result = SimpleNamespace(t1_hit_prob=.6, t2_hit_prob=.3, sl_hit_prob=.2,
        regime='trending', regime_distribution={}, regime_confidence=.9, setup_quality=7,
        is_trap_prob=.1, engine_name='jev', raw_response={}, latency_ms=5000)
    state.rows = {'ml_collection': [state.snapshot], 'ares_signals': [state.signal],
        'llm_consumer_state': [{'live_from': state.snapshot['timestamp'], 'max_signal_age_seconds': 60}],
        'llm_prediction_jobs': [], 'ml_predictions': [], 'llm_predictions': [vars(state.result)]}
    db = MagicMock()
    def table(name):
        query = MagicMock()
        for method in ('select', 'eq', 'in_', 'gte', 'order', 'limit', 'is_', 'upsert', 'range'):
            getattr(query, method).return_value = query
        query.not_ = query
        query.update.side_effect = lambda payload: state.updates.append(payload) or query
        def execute():
            if name in state.table_errors:
                raise state.table_errors[name]
            clock[0] += state.costs.get(name, 0)
            return SimpleNamespace(data=state.rows.get(name, []))
        query.execute.side_effect = execute
        return query
    def rpc(name, params):
        query = MagicMock()
        def execute():
            if name in state.rpc_errors:
                raise state.rpc_errors[name]
            source = {'poll_jev_signals': 'ml_collection', 'read_jev_signal': 'ares_signals',
                      'poll_jev_alert_jobs': 'llm_prediction_jobs'}.get(name)
            if source:
                if source in state.table_errors:
                    raise state.table_errors[source]
                clock[0] += state.costs.get(source, 0)
                rows = state.rows.get(source, [])
                if name == 'poll_jev_signals':
                    rows = [row for row in rows if row.get('signal_uuid')]
                return SimpleNamespace(data=rows)
            if name == 'complete_llm_prediction_job':
                state.published.update(params['p_prediction'])
                # External boundary models the migration's atomic pending queue.
                state.rows['llm_prediction_jobs'] = [{
                    'id': 1, 'signal_uuid': params['p_signal_uuid'], 'prediction_id': 9}]
                state.rows['llm_predictions'] = [dict(state.published)]
            if name == 'record_llm_prediction_latency':
                state.recorded.append(params)
            clock[0] += state.costs.get(name, 0)
            value = {'id': 9} if name == 'complete_llm_prediction_job' else state.job
            return SimpleNamespace(data=state.rpc_data.get(name, value if state.as_dict else [value]))
        query.execute.side_effect = execute
        return query
    db.table.side_effect = table
    db.rpc.side_effect = rpc
    state.db = db
    state.invoke = MagicMock(return_value=state.result)
    state.send = MagicMock(side_effect=lambda *a, **kw: state.alert)
    monkeypatch.setattr(consumer, 'invoke_jev', state.invoke)
    monkeypatch.setattr(consumer, 'send_jev_followup', state.send)
    return state


@pytest.mark.parametrize('rows', [[], [{'signal_uuid': None}]])
def test_empty_or_unbound_poll_never_invokes(flow, rows):
    flow.rows['ml_collection'] = rows
    assert consumer._poll_eligible_signals(flow.db) == []
    flow.invoke.assert_not_called()


def test_snapshot_read_failure_stops_poll_without_inference(flow, caplog):
    flow.table_errors['ml_collection'] = ConnectionError('snapshot unavailable')
    assert consumer._poll_eligible_signals(flow.db) == []
    assert 'snapshot unavailable' in caplog.text
    flow.invoke.assert_not_called()


@pytest.mark.parametrize('missing,error', [(True, None), (False, ConnectionError('signal unavailable'))])
def test_missing_or_unreadable_signal_cannot_dispatch(flow, missing, error):
    if missing:
        flow.rows['ares_signals'] = []
    else:
        flow.table_errors['ares_signals'] = error
    assert consumer.process_signal(flow.db, flow.snapshot) is None
    flow.invoke.assert_not_called()


def test_optional_xgboost_read_failure_does_not_block_independent_jev(flow):
    flow.table_errors['ml_predictions'] = ConnectionError('optional model down')
    assert consumer.process_signal(flow.db, flow.snapshot) == 'COMPLETED'
    assert 'xgboost_comparison' not in flow.published['input_state']
    flow.invoke.assert_called_once()


@pytest.mark.parametrize('error', [None, ConnectionError('already has an active owner'), ConnectionError('database down')])
def test_unacknowledged_claim_never_dispatches(flow, error):
    if error is None:
        flow.rpc_data['claim_llm_prediction_job'] = []
    else:
        flow.rpc_errors['claim_llm_prediction_job'] = error
    assert consumer.process_signal(flow.db, flow.snapshot) is None
    flow.invoke.assert_not_called()
    flow.send.assert_not_called()


def test_lost_invocation_marker_ack_cannot_dispatch(flow):
    flow.rpc_errors['begin_llm_invocation'] = ConnectionError('marker acknowledgment lost')
    assert consumer.process_signal(flow.db, flow.snapshot) is None
    flow.invoke.assert_not_called()


def test_dict_rpc_response_and_missing_poll_timing_remain_valid(flow):
    flow.as_dict = True
    assert consumer.process_signal(flow.db, flow.snapshot) == 'COMPLETED'
    assert flow.published['latency_snapshot_read_ms'] is None
    assert flow.published['latency_signal_read_ms'] == 0
    assert len(flow.recorded) == 1


def test_unacknowledged_archive_does_not_send_or_publish_total(flow):
    flow.rpc_data['complete_llm_prediction_job'] = []
    assert consumer.process_signal(flow.db, flow.snapshot) == 'UNKNOWN'
    flow.send.assert_not_called()
    assert not flow.recorded


def test_status_write_failure_still_does_not_dispatch_bad_context(flow, caplog):
    flow.signal.pop('target_1')
    flow.table_errors['llm_prediction_jobs'] = ConnectionError('status write failed')
    assert consumer.process_signal(flow.db, flow.snapshot) == 'FAILED'
    assert 'status write failed' in caplog.text
    flow.invoke.assert_not_called()


def test_unexpected_provider_failure_stays_unknown_without_replay(flow):
    flow.invoke.side_effect = RuntimeError('invalid response decoding')
    assert consumer.process_signal(flow.db, flow.snapshot) == 'UNKNOWN'
    assert flow.updates[-1]['status'] == 'UNKNOWN'
    flow.invoke.assert_called_once()
    flow.send.assert_not_called()


def test_metrics_write_failure_cannot_repeat_delivery(flow, caplog):
    flow.rpc_errors['record_llm_prediction_latency'] = ConnectionError('metrics acknowledgment lost')
    assert consumer.process_signal(flow.db, flow.snapshot) == 'COMPLETED'
    consumer._recover_pending_alerts(flow.db)
    assert 'metrics acknowledgment lost' in caplog.text
    flow.send.assert_called_once()


@pytest.mark.parametrize('alert', ['SUPPRESSED_EXPIRED', 'DELIVERY_UNKNOWN', 'RETRYABLE', 'NONE'])
def test_unconfirmed_alert_cannot_publish_success_latency(flow, alert):
    flow.alert = alert
    assert consumer.process_signal(flow.db, flow.snapshot) == 'COMPLETED'
    consumer._recover_pending_alerts(flow.db)
    assert flow.recorded[-1]['p_confirmed_sent'] is False


def test_unexpected_delivery_failure_records_only_observed_processing(flow):
    flow.send.side_effect = RuntimeError('delivery failed after persistence')
    assert consumer.process_signal(flow.db, flow.snapshot) == 'COMPLETED'
    consumer._recover_pending_alerts(flow.db)
    assert flow.recorded[-1]['p_confirmed_sent'] is False
    flow.invoke.assert_called_once()


@pytest.mark.parametrize('missing', ['prediction', 'signal'])
def test_recovery_with_missing_rows_cannot_send(flow, missing):
    flow.rows['llm_prediction_jobs'] = [{'id': 1, 'signal_uuid': 'sig', 'prediction_id': 9}]
    flow.rows['llm_predictions' if missing == 'prediction' else 'ares_signals'] = []
    consumer._recover_pending_alerts(flow.db)
    flow.send.assert_not_called()
    flow.invoke.assert_not_called()


def test_restart_delivery_records_end_to_end_without_reinvocation(flow):
    flow.rows['llm_prediction_jobs'] = [{'id': 1, 'signal_uuid': 'sig', 'prediction_id': 9}]
    consumer._recover_pending_alerts(flow.db)
    assert flow.recorded[0]['p_persistence_ms'] is None  # Original measurement is retained by SQL.
    assert flow.recorded[0]['p_confirmed_sent'] is True
    flow.invoke.assert_not_called()
    flow.send.assert_called_once()


class SessionClock(datetime):
    hour = 10
    day = 2
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, cls.day, cls.hour, 35, tzinfo=tz or timezone.utc)


@pytest.fixture
def live_loop(flow, monkeypatch):
    import supabase
    monkeypatch.setenv('SUPABASE_URL', 'http://db.invalid')
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY', 'fake-backend-key')
    monkeypatch.setattr(consumer, 'create_client', MagicMock(return_value=flow.db))
    monkeypatch.setattr(supabase, 'create_client', MagicMock(return_value=flow.db))
    monkeypatch.setattr(consumer, 'datetime', SessionClock)
    monkeypatch.setattr(consumer.time, 'sleep', MagicMock(side_effect=KeyboardInterrupt))
    SessionClock.hour = 10
    SessionClock.day = 2
    return flow


def test_worker_before_open_sleeps_then_honors_interrupt(live_loop):
    SessionClock.hour = 8
    consumer.run()
    consumer.time.sleep.assert_called_once_with(30)
    live_loop.invoke.assert_not_called()


def test_worker_after_close_waits_without_dispatch_or_database_polling(live_loop):
    SessionClock.hour = 16
    consumer.run()
    consumer.time.sleep.assert_called_once_with(30)
    live_loop.invoke.assert_not_called()
    live_loop.db.rpc.assert_not_called()
    assert [call.args[0] for call in live_loop.db.table.call_args_list] == ['llm_consumer_state']


@pytest.mark.parametrize('start_hour', [8, 16])
def test_worker_resumes_when_session_opens(live_loop, monkeypatch, start_hour):
    SessionClock.hour = start_hour
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            if start_hour == 16:
                SessionClock.day = 5  # Next business session after Friday close.
            SessionClock.hour = 10
            return
        raise KeyboardInterrupt
    monkeypatch.setattr(consumer.time, 'sleep', sleep)
    with pytest.raises(KeyboardInterrupt):
        consumer.run()
    assert sleeps == [30, consumer.POLL_INTERVAL]
    live_loop.invoke.assert_called_once()


def test_running_worker_predicts_across_session_close_without_rebootstrap(live_loop, monkeypatch):
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            live_loop.invoke.assert_called_once()
            SessionClock.hour = 16
        elif len(sleeps) == 2:
            live_loop.invoke.assert_called_once()  # No inference or delivery overnight.
            live_loop.send.assert_called_once()
            SessionClock.day = 5
            SessionClock.hour = 10
            live_loop.snapshot.update(signal_uuid='next-session', timestamp='2026-10-05T04:30:00Z')
            live_loop.signal['signal_uuid'] = 'next-session'
        else:
            raise KeyboardInterrupt
    monkeypatch.setattr(consumer.time, 'sleep', sleep)
    with pytest.raises(KeyboardInterrupt):
        consumer.run()
    assert sleeps == [consumer.POLL_INTERVAL, 30, consumer.POLL_INTERVAL]
    assert live_loop.invoke.call_count == live_loop.send.call_count == 2
    assert [call.args[1] for call in live_loop.send.call_args_list] == ['sig', 'next-session']
    assert sum(call.args[0] == 'llm_consumer_state' for call in live_loop.db.table.call_args_list) == 1
    assert sum(call.args[0] == 'recover_llm_prediction_jobs' for call in live_loop.db.rpc.call_args_list) == 2


def test_one_bad_signal_cannot_stop_worker_processing_next_signal(live_loop, caplog):
    live_loop.rows['ml_collection'] = [{'signal_uuid': 'bad'}, live_loop.snapshot]
    with pytest.raises(KeyboardInterrupt):
        consumer.run()  # The test's shutdown interrupt occurs at the loop's outer sleep.
    assert 'Unhandled error processing bad' in caplog.text
    live_loop.invoke.assert_called_once()
    consumer.time.sleep.assert_called_once_with(consumer.POLL_INTERVAL)


def test_database_recovery_error_does_not_stop_polling(live_loop, caplog):
    live_loop.rpc_errors['recover_llm_prediction_jobs'] = ConnectionError('recovery RPC offline')
    with pytest.raises(KeyboardInterrupt):
        consumer.run()
    live_loop.invoke.assert_called_once()


def test_loop_database_failure_is_logged_and_backed_off(live_loop, caplog):
    live_loop.table_errors['llm_prediction_jobs'] = ConnectionError('delivery query offline')
    with pytest.raises(KeyboardInterrupt):
        consumer.run()
    assert 'Consumer loop error' in caplog.text
    consumer.time.sleep.assert_called_once_with(consumer.POLL_INTERVAL)
    live_loop.invoke.assert_called_once()  # Fresh inference runs before delivery recovery fails.


def test_interrupt_during_poll_stops_loop(live_loop):
    live_loop.table_errors['ml_collection'] = KeyboardInterrupt()
    consumer.run()
    live_loop.invoke.assert_not_called()


def test_empty_bootstrap_fails_before_polling(live_loop):
    live_loop.rows['llm_consumer_state'] = []
    with pytest.raises(RuntimeError, match='Failed to bootstrap'):
        consumer.run()
    live_loop.invoke.assert_not_called()


@pytest.mark.parametrize('module', ['system_one', 'system_one.consumer'])
def test_module_entrypoints_wait_at_closed_session(live_loop, monkeypatch, module):
    import datetime as datetime_module
    SessionClock.hour = 16
    monkeypatch.setattr(datetime_module, 'datetime', SessionClock)
    with pytest.warns(RuntimeWarning) if module.endswith('.consumer') else nullcontext():
        runpy.run_module(module, run_name='__main__')
    consumer.time.sleep.assert_called_once_with(30)
    live_loop.invoke.assert_not_called()


@pytest.mark.parametrize('rows', [None, [], [{'signal_uuid': 'fresh', 'snapshot_uuid': 'snap'}]])
def test_poll_uses_database_policy_rpc_without_history_queries(monkeypatch, rows):
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(consumer.time, 'monotonic', lambda: next(clock))
    db = MagicMock()
    db.rpc.return_value.execute.return_value.data = rows
    result = consumer._poll_eligible_signals(db)
    assert result == [dict(row, _jev_snapshot_read_ms=2000) for row in (rows or [])]
    db.rpc.assert_called_once_with('poll_jev_signals', {
        'p_consumer_id': consumer.CONSUMER_ID,
        'p_context_version': consumer.CONTEXT_VERSION,
        'p_question_version': consumer.QUESTION_VERSION,
        'p_model_name': consumer.DEFAULT_MODEL,
    })
    db.table.assert_not_called()


@pytest.mark.parametrize('rows', [None, []])
def test_alert_recovery_uses_database_backoff_rpc_without_dispatch(flow, rows):
    flow.rows['llm_prediction_jobs'] = rows
    consumer._recover_pending_alerts(flow.db)
    flow.db.rpc.assert_called_once_with('poll_jev_alert_jobs', {'p_consumer_id': consumer.CONSUMER_ID})
    flow.db.table.assert_not_called()
    flow.invoke.assert_not_called()
    flow.send.assert_not_called()


def test_recovery_handles_only_one_saved_alert_per_loop(flow):
    flow.rows['llm_prediction_jobs'] = [
        {'id': i, 'signal_uuid': 'sig', 'prediction_id': 9} for i in range(10)]
    consumer._recover_pending_alerts(flow.db)
    flow.send.assert_called_once()
    flow.invoke.assert_not_called()


def test_fresh_inference_precedes_slow_recovery(live_loop, monkeypatch):
    sequence = []
    live_loop.invoke.side_effect = lambda *args, **kwargs: sequence.append('inference') or live_loop.result
    def recover_send(*args, **kwargs):
        sequence.append('recovered' if args else 'new')
        live_loop.clock[0] += 10  # Slow recovered delivery consumes its timeout budget.
        return 'SENT'
    live_loop.send.side_effect = recover_send
    live_loop.rows['llm_prediction_jobs'] = [
        {'id': i, 'signal_uuid': 'sig', 'prediction_id': 9} for i in range(10)]
    with pytest.raises(KeyboardInterrupt):
        consumer.run()
    assert sequence == ['inference', 'recovered']


def test_inference_archives_pending_prediction_without_webhook(flow):
    assert consumer.process_signal(flow.db, flow.snapshot) == 'COMPLETED'
    flow.send.assert_not_called()
    assert flow.rows['llm_prediction_jobs'][0]['prediction_id'] == 9
    assert flow.recorded[-1]['p_delivery_ms'] is None
    assert flow.recorded[-1]['p_confirmed_sent'] is False


def test_burst_finishes_inference_before_any_slow_webhook(live_loop):
    second = dict(live_loop.snapshot, signal_uuid='second', snapshot_uuid='snap-second')
    live_loop.rows['ml_collection'] = [live_loop.snapshot, second]
    sequence = []
    def infer(*args, **kwargs):
        sequence.append('inference')
        return live_loop.result
    def deliver(*args, **kwargs):
        sequence.append('webhook')
        live_loop.clock[0] += 10
        return 'SENT'
    live_loop.invoke.side_effect = infer
    live_loop.send.side_effect = deliver
    with pytest.raises(KeyboardInterrupt):
        consumer.run()
    assert sequence == ['inference', 'inference', 'webhook']
    assert live_loop.invoke.call_count == 2
