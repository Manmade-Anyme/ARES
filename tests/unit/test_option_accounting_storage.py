"""Reference accounting evidence survives serialization and backend failures."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from models import OptionTrailingContext
from storage import AnalyticsLogger


def logger_with_client(client, configured=True):
    logger = AnalyticsLogger.__new__(AnalyticsLogger)
    logger.supabase = client
    logger._option_backend_configured = configured
    return logger


def test_accounting_round_trip_retains_complete_and_missing_evidence():
    accounting = {"legs": {"T1": {"event_at": "2026-10-23T04:40:00Z", "spot_price": 22870.75,
                                  "spot_source": "spot_target", "option_price": None,
                                  "option_source": None, "option_observed_at": None, "approximate": None},
                            "EXIT": {"event_at": "2026-10-23T05:30:00Z", "spot_price": None,
                                     "option_price": 145, "option_source": "option_5m_close"}},
                  "spot_booked_points": 12.5, "option_booked_points": None}
    context = OptionTrailingContext.from_dict({"accounting": accounting, "phase": "CLOSED"})
    assert OptionTrailingContext.from_dict(context.to_dict()).accounting == accounting
    assert OptionTrailingContext.from_dict({}).accounting == {
        "legs": {}, "spot_booked_points": None, "option_booked_points": None}


def test_correction_is_backend_only_and_requires_confirmation():
    client = Mock()
    accounting = {"legs": {"T1": {"option_price": 145}}}
    client.rpc.return_value.execute.return_value = SimpleNamespace(data={"success": True, "revision": 3})
    logger = logger_with_client(client)
    assert logger.apply_option_accounting_correction("trade", 2, "recover", accounting, 3.0, None)["revision"] == 3
    assert client.rpc.call_args.args == ("apply_option_accounting_correction", {
        "p_trade_id": "trade", "p_expected_revision": 2, "p_event_id": "recover",
        "p_accounting": accounting, "p_spot_pnl_points": 3.0, "p_option_pnl_points": None})
    client.table.assert_not_called()
    client.rpc.return_value.execute.return_value = SimpleNamespace(data=None)
    with pytest.raises(RuntimeError, match="did not confirm"):
        logger.apply_option_accounting_correction("trade", 2, "recover", accounting, 3.0, None)
    with pytest.raises(RuntimeError, match="SERVICE_ROLE"):
        logger_with_client(client, False).apply_option_accounting_correction("trade", 2, "recover", accounting, 3.0, None)


@pytest.mark.parametrize("revision,event,accounting", [(0, 'x', {}), (True, 'x', {}), (1, '', {}), (1, 'x', None)])
def test_correction_rejects_malformed_request(revision, event, accounting):
    client = Mock()
    with pytest.raises(ValueError):
        logger_with_client(client).apply_option_accounting_correction('trade', revision, event, accounting, None, None)
    client.rpc.assert_not_called()


def test_capability_check_is_fail_closed():
    client = Mock()
    logger = logger_with_client(client)
    client.rpc.return_value.execute.return_value = SimpleNamespace(data={"ready": True, "schema_version": "option_reference_accounting_v1"})
    assert logger.option_reference_accounting_ready() is True
    client.rpc.return_value.execute.return_value = SimpleNamespace(data={"ready": True, "schema_version": "old"})
    assert logger.option_reference_accounting_ready() is False
    client.rpc.return_value.execute.side_effect = PermissionError("denied")
    assert logger.option_reference_accounting_ready() is False
    assert logger_with_client(client, False).option_reference_accounting_ready() is False


def test_recovery_scan_is_bounded_and_fails_closed():
    client = Mock()
    client.rpc.return_value.execute.return_value = SimpleNamespace(data=[{"score": 1}])
    logger = logger_with_client(client)
    assert logger.list_incomplete_option_accounting(20, 40) == [{"score": 1}]
    assert client.rpc.call_args.args == ("list_incomplete_option_accounting", {"p_limit": 20, "p_offset": 40})
    for limit, offset in [(0, 0), (501, 0), (True, 0), (100, -1)]:
        with pytest.raises(ValueError):
            logger.list_incomplete_option_accounting(limit, offset)
    client.rpc.return_value.execute.return_value = SimpleNamespace(data=None)
    with pytest.raises(RuntimeError, match="invalid data"):
        logger.list_incomplete_option_accounting()
    with pytest.raises(RuntimeError, match="SERVICE_ROLE"):
        logger_with_client(client, False).list_incomplete_option_accounting()


def test_typed_round_trip_preserves_ambiguity_and_protective_observation():
    ambiguity = {"source_minute": "2026-10-23T10:30:00+05:30", "status": "ORDER_UNKNOWN",
                 "protective_option_exit": {"event_at": "2026-10-23T10:35:00+05:30",
                                            "option_close": 145.0, "option_stop": 146.0}}
    resolution = {"outcome": "T2_HIT", "evidence_source": "complete_ticks"}
    restored = OptionTrailingContext.from_dict({"ambiguous_index_touch": ambiguity,
                                                 "ambiguity_resolution": resolution}).to_dict()
    assert restored["ambiguous_index_touch"] == ambiguity
    assert restored["ambiguity_resolution"] == resolution


def test_conflict_state_read_does_not_require_new_column_on_legacy_database():
    client = Mock()
    analytics = Mock()
    active = Mock()
    client.table.side_effect = lambda name: analytics if name == "trade_analytics" else active
    analytics.select.return_value.eq.return_value.execute.return_value = SimpleNamespace(data=[{
        "market_context": {"option_trailing": {"policy_version": "option_atr_v1"}},
        "result_state": "OPEN", "score": 1, "pnl_points": None}])
    active.select.return_value.eq.return_value.execute.return_value = SimpleNamespace(data=[{"state": "T1_HIT", "stop_loss": 1}])
    assert logger_with_client(client).get_option_lifecycle_state("trade")["score"] == 1
    assert "option_pnl_points" not in analytics.select.call_args.args[0]


def test_ml_sync_is_backend_only_and_requires_confirmed_rpc():
    client = Mock()
    logger = logger_with_client(client)
    client.rpc.return_value.execute.return_value = SimpleNamespace(data={"success": True, "matched": 1})
    assert logger.sync_option_reference_ml_label("trade")["matched"] == 1
    assert client.rpc.call_args.args == ("sync_option_reference_ml_label", {"p_trade_id": "trade"})
    client.table.assert_not_called()
    client.rpc.return_value.execute.return_value = SimpleNamespace(data=None)
    with pytest.raises(RuntimeError, match="did not confirm"):
        logger.sync_option_reference_ml_label("trade")
    with pytest.raises(RuntimeError, match="SERVICE_ROLE"):
        logger_with_client(client, False).sync_option_reference_ml_label("trade")
