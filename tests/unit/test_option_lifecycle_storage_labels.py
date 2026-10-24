"""Persistence failures cannot bypass the transaction; versioned labels retain milestones."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from storage import AnalyticsLogger
from ml_signal.labeling import label_from_ares_outcome
from ml_signal.backfill_labels import repair_stuck_open_trades
from tests.unit.test_task194_ml_labels_and_oi_distribution import _FakeSupabase


def logger_with_client(client):
    logger = AnalyticsLogger.__new__(AnalyticsLogger)
    logger.supabase = client
    logger._option_backend_configured = True  # Explicit offline RPC adapter.
    return logger


def test_rpc_failure_propagates_without_any_direct_table_writes():
    client = Mock()
    client.rpc.return_value.execute.side_effect = PermissionError("backend only")
    with pytest.raises(PermissionError):
        logger_with_client(client).apply_option_lifecycle_transition("trade", 1, "T1:time", {})
    client.table.assert_not_called()


@pytest.mark.parametrize("revision,event", [(None, "event"), (0, "event"), (True, "event"), (1, ""), (1, None)])
def test_transaction_requires_positive_revision_and_stable_event(revision, event):
    client = Mock()
    with pytest.raises(ValueError):
        logger_with_client(client).apply_option_lifecycle_transition("trade", revision, event, {})
    client.rpc.assert_not_called()


def test_empty_rpc_response_does_not_mean_commit_success():
    client = Mock()
    client.rpc.return_value.execute.return_value = SimpleNamespace(data=None)
    with pytest.raises(RuntimeError):
        logger_with_client(client).apply_option_lifecycle_transition("trade", 1, "event", {})
    client.table.assert_not_called()


def test_alert_ack_uses_dedicated_rpc_without_lifecycle_mutation():
    client = Mock()
    client.rpc.return_value.execute.return_value = SimpleNamespace(data={"success": True})
    logger_with_client(client).acknowledge_option_lifecycle_alert("trade", "event", "AMBIGUOUS", "timeout")
    assert client.rpc.call_args.args[0] == "acknowledge_option_lifecycle_alert"
    assert client.rpc.call_args.args[1]["p_status"] == "AMBIGUOUS"
    client.table.assert_not_called()


def record(result="OPTION_SL_FLOOR_HIT", milestone=2, remaining=0):
    return {"id": "option", "result_state": result, "entry_timestamp": "2026-10-23T05:00:00Z",
            "entry_price": 22434.8, "direction": "BEARISH", "score": 2,
            "market_context": {"option_trailing": {"policy_version": "option_atr_v1",
                               "highest_milestone": milestone, "remaining_fraction": remaining}}}


@pytest.mark.parametrize("reason", ["ATR_SL_HIT", "OPTION_SL_FLOOR_HIT"])
def test_t2_terminal_label_is_win_even_when_t1_only_policy_is_loss(reason):
    labeled = label_from_ares_outcome([record(reason)], t1_is_win=False)
    assert labeled["label"].tolist() == [1]


def test_open_runner_score_two_never_becomes_training_label():
    assert label_from_ares_outcome([record("OPEN", remaining=.2)]).empty


def test_terminal_name_with_nonzero_allocation_is_not_final_training_label():
    assert label_from_ares_outcome([record(remaining=.2)]).empty


def test_repair_retains_t2_floor_score_and_unknown_index_exit():
    rows = {"trade_analytics": [record("OPEN", remaining=.2)], "active_trades": [{
        "id": "option", "state": "CLOSED", "exit_type": "OPTION_SL_FLOOR_HIT",
        "exit_price": None, "exit_timestamp": "2026-10-23T06:00:00Z", "pnl_points_override": None}]}
    assert repair_stuck_open_trades(_FakeSupabase(rows), apply=True) == 1
    trade = rows["trade_analytics"][0]
    assert (trade["score"], trade["exit_price"], trade["pnl_points"]) == (2, None, None)


def test_real_training_consumer_uses_canonical_t2_exit_and_rejects_open_runner():
    from ml_signal.pipeline_trade_outcomes import TradeOutcomePipeline
    times = {"entry_timestamp": "2026-10-23T04:40:00Z", "exit_timestamp": "2026-10-23T06:00:00Z"}
    closed = record()
    opened = record("OPEN", remaining=.2)
    rows = [{"trade_id": ident, "timestamp": "2026-10-23T04:40:00Z",
             "trade_outcome": "OPTION_SL_FLOOR_HIT", "raw_candle": {"close": 22434.8}}
            for ident in ("closed", "open")]
    metadata = {"closed": {**times, "option_trailing": closed["market_context"]["option_trailing"],
                           "result_state": closed["result_state"]},
                "open": {**times, "option_trailing": opened["market_context"]["option_trailing"],
                         "result_state": "OPEN"}}
    frame = TradeOutcomePipeline().prepare_dataset(rows, metadata)
    assert frame["trade_id"].tolist() == ["closed"]
    assert frame["label"].tolist() == [1]


def test_missing_backend_credential_rejects_rpc_before_network():
    client = Mock()
    logger = logger_with_client(client)
    logger._option_backend_configured = False
    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_ROLE_KEY"):
        logger.apply_option_lifecycle_transition("trade", 1, "event", {})
    client.rpc.assert_not_called()


def test_legacy_be_repair_never_replaces_new_policy_weighted_pnl():
    from ml_signal.backfill_labels import repair_be_after_t1
    trade = record("STOPPED_OUT_AT_BE", milestone=1)
    trade["pnl_points"] = 0
    rows = {"trade_analytics": [trade], "active_trades": [{"id": "option", "target_1": 22409.8}],
            "ares_signals": [], "ml_collection": []}
    assert repair_be_after_t1(_FakeSupabase(rows), apply=True) == 0
    assert trade["pnl_points"] == 0


def test_unknown_terminal_reason_is_not_a_training_label_even_after_t2():
    assert label_from_ares_outcome([record("UNRECOGNIZED")]).empty


def test_missing_canonical_floor_trade_is_left_for_evidence_based_repair():
    rows = {"trade_analytics": [], "active_trades": [{"id": "unknown", "exit_type": "OPTION_SL_FLOOR_HIT",
            "exit_price": None, "exit_timestamp": "2026-10-23T06:00:00Z"}]}
    assert repair_stuck_open_trades(_FakeSupabase(rows), apply=True) == 0
    assert rows["trade_analytics"] == []


def test_canonical_terminal_outcome_and_weighted_pnl_do_not_wait_for_legacy_ml_backfill():
    from ml_signal.pipeline_trade_outcomes import TradeOutcomePipeline
    terminal = record()
    rows = [{"trade_id":"closed", "timestamp":"2026-10-23T04:40:00Z",
             "trade_outcome":None, "trade_pnl":None, "raw_candle":{"close":22434.8}}]
    metadata = {"closed": {"entry_timestamp":"2026-10-23T04:40:00Z",
                           "exit_timestamp":"2026-10-23T06:00:00Z", "trade_pnl":35,
                           "option_trailing":terminal["market_context"]["option_trailing"],
                           "result_state":"OPTION_SL_FLOOR_HIT"}}
    frame = TradeOutcomePipeline().prepare_dataset(rows, metadata)
    assert frame["label"].tolist() == [1]
    assert frame["pnl_points"].tolist() == [35]
