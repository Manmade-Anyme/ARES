import math
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
import httpx
from typesafe_sdk import TypeSafeError, TypeSafeAPITimeoutError

from system_one.context import build_context, validate_barrier_geometry, BarrierValidationError, _safe_float
from system_one.jev import invoke_jev, JevResult, build_questions
from system_one.discord import format_jev_followup, send_jev_followup
from system_one.consumer import _bootstrap_consumer_state, _is_trading_session, _poll_eligible_signals, process_signal, _expire_stale_jobs, _persist_prediction, _recover_pending_alerts
from system_one import CONTEXT_VERSION, QUESTION_VERSION

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_signal_row():
    return {
        "signal_uuid": "sig-123",
        "direction": "BULLISH",
        "spot_at_signal": 100.0,
        "trigger_price": 100.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "stop_loss": 90.0,
        "setup_type": "BREAKOUT",
        "confidence": "HIGH",
        "display_id": "ABC",
        "reasons": []
    }

@pytest.fixture
def base_snapshot_row():
    return {
        "timestamp": "2026-10-01T10:00:00Z",
        "spot": 100.0,
        "candle_features": {"body_ratio": 0.8},
        "volume_features": {"vol_ratio": 1.2},
        "iv_features": {"iv_current": 15.0},
        "oi_features": {"pcr_oi": 1.1},
        "structure_features": {"levels_above": 2.0},
        "meta_features": {},
        "raw_candle": {"open": 98.0, "high": 101.0, "low": 97.0, "close": 100.0},
        "raw_atm_oi": {"ce": {"iv": 14.0, "delta": 0.5, "oi": 1000}, "pe": {"iv": 15.0, "delta": 0.5, "oi": 1200}},
        "oi_wall_context": {"wall_strike": 105.0, "wall_option_type": "CE", "wall_oi": 5000, "wall_oi_change_pct": 0.1, "persistence_snapshots": 5}
    }

@pytest.fixture
def mock_supabase():
    mock = MagicMock()
    return mock


def fresh_delivery():
    """Response fields returned by the database freshness/attempt RPCs."""
    return {"is_fresh": True, "remaining_seconds": 20, "alert_attempt_token": "attempt-1",
            "alert_payload": {"embeds": []}, "alert_destination": "http://wh"}


def claimed_invocation():
    return {"owner_token": "token-1", "id": 1, "invocation_token": "invocation-1",
            "invocation_started_at": "2026-10-01T04:30:00Z",
            "invocation_dispatch_deadline_at": "2026-10-01T04:30:30Z"}

# ---------------------------------------------------------------------------
# Context Tests
# ---------------------------------------------------------------------------

def test_valid_bullish_geometry():
    validate_barrier_geometry("BULLISH", 100.0, 110.0, 120.0, 90.0)

def test_valid_bearish_geometry():
    validate_barrier_geometry("BEARISH", 100.0, 90.0, 80.0, 110.0)

def test_invalid_geometry_raises():
    with pytest.raises(BarrierValidationError):
        validate_barrier_geometry("BULLISH", 100.0, 90.0, 120.0, 80.0)

def test_missing_required_fields(base_signal_row, base_snapshot_row):
    base_signal_row.pop("target_1")
    with pytest.raises(ValueError):
        build_context(base_signal_row, base_snapshot_row)

def test_nan_inf_treated_as_none():
    assert _safe_float(float("nan")) is None
    assert _safe_float(float("inf")) is None
    assert _safe_float("invalid") is None

def test_missing_feature_groups_produce_none(base_signal_row, base_snapshot_row):
    base_snapshot_row["candle_features"] = None
    ctx = build_context(base_signal_row, base_snapshot_row)
    assert ctx["candle"] is None

def test_full_context_building(base_signal_row, base_snapshot_row):
    xgboost_row = {"probability": 0.85, "confidence_tier": "HIGH", "model_version": "v1"}
    ctx = build_context(base_signal_row, base_snapshot_row, xgboost_row)
    
    # Assert entry price and stop price
    assert ctx["signal"]["entry_price"] == 100.0
    assert ctx["signal"]["post_t1_stop_price"] == 100.0
    
    # Assert features
    assert ctx["candle"]["body_ratio"] == 0.8
    assert ctx["xgboost_comparison"]["probability"] == 0.85
    assert ctx["oi_wall"]["wall_strike"] == 105.0
    
    # Wick profile computation
    assert ctx["wick_profile"]["body_pct"] == 0.5  # (100-98)/4 = 0.5
    assert ctx["wick_profile"]["is_green"] is True

# ---------------------------------------------------------------------------
# Jev Tests
# ---------------------------------------------------------------------------

@patch('system_one.jev.TypeSafeClient')
def test_successful_invocation(mock_ts_client):
    mock_client_instance = MagicMock()
    mock_ts_client.return_value.__enter__.return_value = mock_client_instance
    
    mock_response = MagicMock()
    mock_response.choices = {
        "first_barrier": MagicMock(choice="t1_first", probabilities={"t1_first": 0.6, "sl_first": 0.3}, confidence=0.8),
        "t2_given_t1": MagicMock(choice="t2_hits", probabilities={"t2_hits": 0.5}, confidence=0.7),
        "market_regime": MagicMock(choice="trending", probabilities={"trending": 0.9}, confidence=0.9)
    }
    mock_response.nouls = {
        "is_trap": MagicMock(noul=0.1)
    }
    mock_response.scores = {
        "price_action_strength": MagicMock(score=3.0, confidence=0.8, probabilities={}, legend={"a":0,"b":1,"c":2,"d":3,"e":4}),
        "structural_clarity": MagicMock(score=4.0, confidence=0.8, probabilities={}, legend={"a":0,"b":1,"c":2,"d":3,"e":4}),
        "confluence_rating": MagicMock(score=2.0, confidence=0.8, probabilities={}, legend={"a":0,"b":1,"c":2,"d":3,"e":4})
    }
    mock_response.model = "jev-test"
    mock_response.request_id = "req-1"
    mock_response.usage.input_tokens = 10
    mock_response.usage.output_tokens = 10
    mock_client_instance.system_one.return_value = mock_response

    state = {"fake": "state"}
    res = invoke_jev(state)
    
    # Prob invariant: t1 + sl <= 1.0
    assert res.t1_hit_prob + res.sl_hit_prob <= 1.0
    # T2 given T1
    assert res.t2_hit_prob == 0.3  # 0.6 * 0.5
    # T2 <= T1
    assert res.t2_hit_prob <= res.t1_hit_prob
    # Range
    assert 0 <= res.t1_hit_prob <= 1
    
    # 3.0*(10/4)*0.4 + 4.0*(10/4)*0.4 + 2.0*(10/4)*0.2 = 3.0 + 4.0 + 1.0 = 8.0
    assert res.setup_quality == 8.0
    assert res.engine_name == "jev-test"
    assert res.question_version == QUESTION_VERSION
    assert "usage" in res.raw_response
    
    # Check max_retries = 0
    mock_ts_client.assert_called_once()
    assert mock_ts_client.call_args[1]["retry"].max_retries == 0

@patch('system_one.jev.TypeSafeClient')
def test_jev_timeout_error(mock_ts_client):
    mock_client_instance = MagicMock()
    mock_ts_client.return_value.__enter__.return_value = mock_client_instance
    mock_client_instance.system_one.side_effect = TypeSafeAPITimeoutError("timeout")
    
    with pytest.raises(TypeSafeAPITimeoutError):
        invoke_jev({})

# ---------------------------------------------------------------------------
# Discord Tests
# ---------------------------------------------------------------------------

def test_format_jev_followup():
    payload = format_jev_followup("SIG-1", "BREAKOUT", "BULLISH", 0.6, 0.3, 0.3, "trending", 0.9, 8.0, 0.1, "jev-1")
    assert "embeds" in payload
    assert payload["embeds"][0]["color"] == 3447003

@patch('system_one.discord.httpx.Client')
def test_send_jev_followup_success(mock_httpx, mock_supabase):
    mock_client_instance = MagicMock()
    mock_httpx.return_value.__enter__.return_value = mock_client_instance
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "msg-123"}
    mock_client_instance.post.return_value = mock_response

    # Mock freshness check
    mock_supabase.rpc.return_value.execute.return_value.data = [fresh_delivery()]
    # Mock transition
    mock_supabase.table().update().eq().in_().execute.return_value.data = [{"id": 1}]

    res = send_jev_followup(mock_supabase, "sig-1", "SIG-1", "BRK", "BULLISH", 0.6, 0.3, 0.3, "trend", 0.9, 8.0, 0.1, "jev", 1, "http://wh")
    assert res == "SENT"
    mock_client_instance.post.assert_called_once()
    assert mock_client_instance.post.call_args.kwargs["params"] == {"wait": "true"}

@patch('system_one.discord.httpx.Client')
def test_send_jev_followup_rate_limit(mock_httpx, mock_supabase):
    mock_client_instance = MagicMock()
    mock_httpx.return_value.__enter__.return_value = mock_client_instance
    
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {"Retry-After": "5"}
    mock_response.json.return_value = {"retry_after": 5}
    mock_client_instance.post.return_value = mock_response

    mock_supabase.rpc.return_value.execute.return_value.data = [fresh_delivery()]
    mock_supabase.table().update().eq().in_().execute.return_value.data = [{"id": 1}]

    res = send_jev_followup(mock_supabase, "sig-1", "SIG-1", "BRK", "BULLISH", 0.6, 0.3, 0.3, "trend", 0.9, 8.0, 0.1, "jev", 1, "http://wh")
    assert res == "RETRYABLE"

@patch('system_one.discord.httpx.Client')
def test_send_jev_followup_stale(mock_httpx, mock_supabase):
    mock_supabase.rpc.return_value.execute.return_value.data = [{"is_fresh": False, "reason": "expired"}]

    res = send_jev_followup(mock_supabase, "sig-1", "SIG-1", "BRK", "BULLISH", 0.6, 0.3, 0.3, "trend", 0.9, 8.0, 0.1, "jev", 1, "http://wh")
    assert res == "SUPPRESSED_EXPIRED"

@patch('system_one.discord.httpx.Client')
def test_send_jev_followup_timeout(mock_httpx, mock_supabase):
    mock_client_instance = MagicMock()
    mock_httpx.return_value.__enter__.return_value = mock_client_instance
    mock_client_instance.post.side_effect = httpx.TimeoutException("timeout")

    mock_supabase.rpc.return_value.execute.return_value.data = [fresh_delivery()]
    mock_supabase.table().update().eq().in_().execute.return_value.data = [{"id": 1}]

    res = send_jev_followup(mock_supabase, "sig-1", "SIG-1", "BRK", "BULLISH", 0.6, 0.3, 0.3, "trend", 0.9, 8.0, 0.1, "jev", 1, "http://wh")
    assert res == "DELIVERY_UNKNOWN"

# ---------------------------------------------------------------------------
# Consumer Tests
# ---------------------------------------------------------------------------

def test_bootstrap_consumer_state(mock_supabase):
    # First time: select returns empty
    mock_supabase.table().select().eq().execute.return_value.data = []
    # Upsert returns new row
    mock_supabase.table().upsert().execute.return_value.data = [{"live_from": "2026-10-01T00:00:00Z"}]
    
    state = _bootstrap_consumer_state(mock_supabase)
    assert state["live_from"] == "2026-10-01T00:00:00Z"

def test_bootstrap_consumer_state_existing(mock_supabase):
    # Second time: select returns existing
    mock_supabase.table().select().eq().execute.return_value.data = [{"live_from": "2025-01-01T00:00:00Z"}]
    
    state = _bootstrap_consumer_state(mock_supabase)
    assert state["live_from"] == "2025-01-01T00:00:00Z"

@patch('system_one.consumer.datetime')
def test_session_hours(mock_datetime):
    ist = timezone(timedelta(hours=5, minutes=30))
    
    # 10:00 IST -> True
    mock_datetime.now.return_value = datetime(2026, 10, 1, 10, 0, tzinfo=ist)
    assert _is_trading_session() is True

    # 16:00 IST -> False
    mock_datetime.now.return_value = datetime(2026, 10, 1, 16, 0, tzinfo=ist)
    assert _is_trading_session() is False

    # 9:00 IST -> False
    mock_datetime.now.return_value = datetime(2026, 10, 1, 9, 0, tzinfo=ist)
    assert _is_trading_session() is False

def test_poll_eligible_signals(mock_supabase):
    mock_supabase.table().select().not_.is_().gte().order().limit().execute.return_value.data = [
        {"signal_uuid": "sig-1", "timestamp": "2026-10-01"},
        {"signal_uuid": "sig-2", "timestamp": "2026-10-01"}
    ]
    # sig-1 already has job
    mock_supabase.table().select().in_().execute.return_value.data = [{"signal_uuid": "sig-1"}]
    
    eligible = _poll_eligible_signals(mock_supabase, "2026-01-01", 60)
    assert len(eligible) == 1
    assert eligible[0]["signal_uuid"] == "sig-2"

@patch('system_one.consumer.invoke_jev')
@patch('system_one.consumer.send_jev_followup')
def test_process_signal_success(mock_send, mock_invoke, mock_supabase, base_signal_row, base_snapshot_row):
    mock_supabase.table().select().eq().execute.return_value.data = [base_signal_row]
    # mock xgboost row
    mock_supabase.table().select().eq().limit().execute.return_value.data = []
    
    # mock claim
    mock_supabase.rpc.return_value.execute.return_value.data = [claimed_invocation()]
    # mock transition to invoking
    mock_supabase.table().update().eq().eq().eq().execute.return_value.data = [{"id": 1}]
    
    mock_result = MagicMock()
    mock_invoke.return_value = mock_result
    
    # mock persist
    mock_supabase.table().insert().execute.return_value.data = [{"id": 100}]

    status = process_signal(mock_supabase, {"signal_uuid": "sig-1", "timestamp": "time", "snapshot_uuid": "snap-1"})
    assert status == "COMPLETED"

@patch('system_one.consumer.invoke_jev')
def test_process_signal_context_error(mock_invoke, mock_supabase, base_signal_row, base_snapshot_row):
    base_signal_row.pop("target_1")  # Make invalid
    mock_supabase.table().select().eq().execute.return_value.data = [base_signal_row]
    mock_supabase.rpc.return_value.execute.return_value.data = [claimed_invocation()]
    mock_supabase.table().update().eq().eq().eq().execute.return_value.data = [{"id": 1}]

    status = process_signal(mock_supabase, {"signal_uuid": "sig-1", "timestamp": "time", "snapshot_uuid": "snap-1"})
    assert status == "FAILED"

@patch('system_one.consumer.invoke_jev')
def test_process_signal_jev_timeout(mock_invoke, mock_supabase, base_signal_row, base_snapshot_row):
    mock_supabase.table().select().eq().execute.return_value.data = [base_signal_row]
    mock_supabase.rpc.return_value.execute.return_value.data = [claimed_invocation()]
    mock_supabase.table().update().eq().eq().eq().execute.return_value.data = [{"id": 1}]
    
    mock_invoke.side_effect = TypeSafeAPITimeoutError("timeout")

    status = process_signal(mock_supabase, {"signal_uuid": "sig-1", "timestamp": "time", "snapshot_uuid": "snap-1"})
    assert status == "UNKNOWN"

def test_expire_stale_jobs(mock_supabase):
    _expire_stale_jobs(mock_supabase)
    mock_supabase.rpc.assert_called_once_with("recover_llm_prediction_jobs", {"p_consumer_id": "jev-primary"})


@patch('system_one.consumer.invoke_jev')
def test_expired_before_dispatch_never_calls_jev(mock_invoke, mock_supabase, base_signal_row, base_snapshot_row):
    """A claim can expire while context is read; the final database gate must deny dispatch."""
    mock_supabase.table().select().eq().execute.return_value.data = [base_signal_row]
    mock_supabase.table().select().eq().limit().execute.return_value.data = []
    def rpc(name, params):
        query = MagicMock()
        query.execute.return_value.data = (
            [{"owner_token": "owner", "id": 1}] if name == "claim_llm_prediction_job" else []
        )
        return query
    mock_supabase.rpc.side_effect = rpc
    mock_supabase.table().update().eq().eq().eq().execute.return_value.data = [{"id": 1}]
    snapshot = dict(base_snapshot_row, signal_uuid="sig-123", snapshot_uuid="snap-1")
    process_signal(mock_supabase, snapshot)
    mock_invoke.assert_not_called()


@patch('system_one.discord.httpx.Client')
def test_discord_server_error_has_uncertain_delivery(mock_httpx, mock_supabase):
    """A server error cannot prove that Discord did not create the message."""
    mock_supabase.rpc.return_value.execute.return_value.data = [fresh_delivery()]
    mock_supabase.table().update().eq().in_().execute.return_value.data = [{"id": 1}]
    response = mock_httpx.return_value.__enter__.return_value.post.return_value
    response.status_code = 503
    result = send_jev_followup(mock_supabase, "sig-1", "SIG-1", "BRK", "BULLISH", .6, .3, .3, "trend", .9, 8., .1, "jev", 1, "http://wh")
    assert result == "DELIVERY_UNKNOWN"


def send_fixture_alert(db):
    return send_jev_followup(db, "sig-1", "SIG-1", "BRK", "BULLISH", .6, .3, .3,
                             "trend", .9, 8., .1, "jev", 1, "http://wh")


def rpc_result(data):
    result = MagicMock()
    result.execute.return_value.data = data
    return result


@pytest.mark.parametrize("bad_gate", [None, [], {}, {"is_fresh": True},
    {"is_fresh": True, "remaining_seconds": float("nan")},
    {"is_fresh": True, "remaining_seconds": -1},
    {"is_fresh": False, "reason": "backoff"}])
@patch('system_one.discord.httpx.Client')
def test_missing_or_unusable_database_gate_never_sends(mock_httpx, mock_supabase, bad_gate):
    mock_supabase.rpc.return_value.execute.return_value.data = bad_gate
    assert send_fixture_alert(mock_supabase) == "NONE"
    mock_httpx.assert_not_called()
    mock_supabase.table.assert_not_called()


@patch('system_one.discord.httpx.Client')
def test_unacknowledged_sending_marker_never_sends(mock_httpx, mock_supabase):
    def rpc(name, params):
        if name == "begin_jev_alert_attempt":
            raise ConnectionError("marker may have committed; acknowledgment lost")
        return rpc_result(fresh_delivery())
    mock_supabase.rpc.side_effect = rpc
    assert send_fixture_alert(mock_supabase) == "NONE"
    mock_httpx.assert_not_called()


@patch('system_one.discord.httpx.Client')
def test_expiry_after_sending_marker_prevents_transport(mock_httpx, mock_supabase):
    mock_supabase.rpc.side_effect = [rpc_result(fresh_delivery()), rpc_result([fresh_delivery()]),
        rpc_result({"is_fresh": False, "reason": "expired"})]
    assert send_fixture_alert(mock_supabase) == "SUPPRESSED_EXPIRED"
    mock_httpx.return_value.__enter__.return_value.post.assert_not_called()


@pytest.mark.parametrize("failure", [httpx.ReadError("accepted but response lost"),
                                    httpx.TimeoutException("timeout")])
@patch('system_one.discord.httpx.Client')
def test_uncertain_delivery_is_never_resent_on_restart(mock_httpx, mock_supabase, failure):
    client = mock_httpx.return_value.__enter__.return_value
    client.post.side_effect = failure
    mock_supabase.rpc.side_effect = [rpc_result(fresh_delivery()), rpc_result([fresh_delivery()]),
        rpc_result(fresh_delivery()), rpc_result({"is_fresh": False, "reason": "alert_not_pending", "alert_status": "DELIVERY_UNKNOWN"})]
    assert send_fixture_alert(mock_supabase) == "DELIVERY_UNKNOWN"
    assert send_fixture_alert(mock_supabase) == "NONE"
    client.post.assert_called_once()


@patch('system_one.discord.httpx.Client')
def test_success_persistence_retries_do_not_repeat_webhook(mock_httpx, mock_supabase):
    mock_supabase.rpc.return_value.execute.return_value.data = [fresh_delivery()]
    client = mock_httpx.return_value.__enter__.return_value
    client.post.return_value.status_code = 200
    client.post.return_value.json.return_value = {"id": "created-message"}
    persist = mock_supabase.table().update().eq().eq().in_().execute
    persist.side_effect = [ConnectionError("lost commit acknowledgment"), MagicMock(data=[{"id": 1}])]
    assert send_fixture_alert(mock_supabase) == "SENT"
    client.post.assert_called_once()


@patch('system_one.discord.httpx.Client')
def test_missing_success_confirmation_stays_unknown(mock_httpx, mock_supabase):
    mock_supabase.rpc.return_value.execute.return_value.data = [fresh_delivery()]
    client = mock_httpx.return_value.__enter__.return_value
    client.post.return_value.status_code = 200
    client.post.return_value.json.return_value = {}
    assert send_fixture_alert(mock_supabase) == "DELIVERY_UNKNOWN"


@patch('system_one.discord.httpx.Client')
def test_transport_timeout_uses_remaining_window(mock_httpx, mock_supabase):
    gate = dict(fresh_delivery(), remaining_seconds=.5)
    mock_supabase.rpc.return_value.execute.return_value.data = [gate]
    client = mock_httpx.return_value.__enter__.return_value
    client.post.return_value.status_code = 200
    client.post.return_value.json.return_value = {"id": "message"}
    send_fixture_alert(mock_supabase)
    assert 0 < client.timeout.connect <= .5


@patch('system_one.consumer.send_jev_followup')
@patch('system_one.consumer.invoke_jev')
def test_prediction_persistence_failure_cannot_send_alert(mock_invoke, mock_send, mock_supabase, base_signal_row, base_snapshot_row):
    mock_supabase.table().select().eq().execute.return_value.data = [base_signal_row]
    mock_supabase.table().select().eq().limit().execute.return_value.data = []
    mock_supabase.rpc.return_value.execute.return_value.data = [claimed_invocation()]
    def rpc(name, params):
        if name == "complete_llm_prediction_job":
            raise ConnectionError("database unavailable")
        return rpc_result([claimed_invocation()])
    mock_supabase.rpc.side_effect = rpc
    snapshot = dict(base_snapshot_row, signal_uuid="sig-123", snapshot_uuid="snap-1")
    assert process_signal(mock_supabase, snapshot) == "UNKNOWN"
    mock_invoke.assert_called_once()
    mock_send.assert_not_called()


def test_concurrent_bootstrap_loads_existing_watermark(mock_supabase):
    load = mock_supabase.table().select().eq().execute
    load.side_effect = [MagicMock(data=[]), MagicMock(data=[{"live_from": "original-cutoff"}])]
    mock_supabase.table().upsert().execute.return_value.data = []
    assert _bootstrap_consumer_state(mock_supabase)["live_from"] == "original-cutoff"
    assert mock_supabase.table().upsert.call_args.kwargs["ignore_duplicates"] is True


def test_prediction_commit_ack_retry_reuses_response_and_invocation(mock_supabase):
    mock_supabase.rpc.return_value.execute.side_effect = [
        ConnectionError("commit acknowledgment lost"), MagicMock(data=[{"id": 7}])]
    result = MagicMock()
    assert _persist_prediction(mock_supabase, "sig-1", result, {"state": "fixed"},
        "snap-1", "event", "invoked", "invocation-1", 1, 2, 3) == 7
    first, second = mock_supabase.rpc.call_args_list
    assert first == second
    assert first.args[0] == "complete_llm_prediction_job"
    assert first.args[1]["p_invocation_token"] == "invocation-1"
    mock_supabase.table.assert_not_called()


@patch('system_one.consumer.send_jev_followup')
@patch('system_one.consumer.invoke_jev')
def test_restart_pending_alert_uses_saved_prediction(mock_invoke, mock_send, mock_supabase, base_signal_row):
    mock_supabase.table().select().eq().eq().in_().limit().execute.return_value.data = [
        {"id": 1, "prediction_id": 7, "signal_uuid": "sig-123"}]
    saved = {"t1_hit_prob": .6, "t2_hit_prob": .3, "sl_hit_prob": .2,
        "regime": "trending", "regime_confidence": .9, "setup_quality": 7,
        "is_trap_prob": .1, "engine_name": "original-model"}
    mock_supabase.table().select().eq().execute.side_effect = [
        MagicMock(data=[saved]), MagicMock(data=[base_signal_row])]
    _recover_pending_alerts(mock_supabase)
    mock_invoke.assert_not_called()
    mock_send.assert_called_once()
    assert mock_send.call_args.args[-2:] == ("original-model", 1)


@patch('system_one.consumer.invoke_jev')
def test_slow_invocation_ack_cannot_start_stale_inference(mock_invoke, mock_supabase, base_signal_row, base_snapshot_row):
    clock = [0.0]
    mock_supabase.table().select().eq().execute.return_value.data = [base_signal_row]
    mock_supabase.table().select().eq().limit().execute.return_value.data = []
    def rpc(name, params):
        job = claimed_invocation()
        if name == "begin_llm_invocation":
            job["invocation_dispatch_deadline_at"] = "2026-10-01T04:30:01Z"
            clock[0] = 2.0  # Slow acknowledgment consumes the entire one-second budget.
        return rpc_result([job])
    mock_supabase.rpc.side_effect = rpc
    with patch('system_one.consumer.time.monotonic', side_effect=lambda: clock[0]):
        assert process_signal(mock_supabase, dict(base_snapshot_row, signal_uuid="sig-123")) == "UNKNOWN"
    mock_invoke.assert_not_called()


@patch('system_one.jev.TypeSafeClient')
def test_sdk_setup_cannot_start_stale_request(mock_client):
    clock = [0.0]
    def enter():
        clock[0] = 2.0
        return mock_client.return_value
    mock_client.return_value.__enter__.side_effect = enter
    with patch('system_one.jev.time.monotonic', side_effect=lambda: clock[0]):
        with pytest.raises(ValueError, match="dispatch window elapsed"):
            invoke_jev({}, dispatch_deadline=1.0)
    mock_client.return_value.system_one.assert_not_called()
