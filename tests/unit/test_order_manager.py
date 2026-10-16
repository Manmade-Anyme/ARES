"""Execution contracts; fake SDK responses never contact an account."""
from dataclasses import asdict, replace
import hashlib
import json
from unittest.mock import MagicMock

import pytest

from models import OptionGreeks, OptionOrderBracket
from execution.order_manager import DhanSuperOrderAdapter, OrderManager, OrderRecord


def bracket(**changes):
    base = OptionOrderBracket(
        security_id="123", expiry_date="2099-01-01", strike=24000,
        option_type="CE", entry_limit=100.0, synthetic_spot_sl=23990.0,
        catastrophic_opt_sl=90.0, target_1=110.0, target_2=120.0,
        lots=2, greeks=OptionGreeks(.5, .001, -10, 2, 15), is_valid=True,
        signal_id="signal-123", lot_size=65, spot_entry=24000,
        spot_target_1=24020, spot_target_2=24040,
    )
    return replace(base, **changes)


def test_submission_does_not_create_a_filled_position_and_duplicate_is_idempotent():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket())
    assert record.status == "SUBMITTED"
    assert not record.ready_to_register
    assert manager.dispatch_bracket(bracket()) is record
    assert record.filled_quantity == 0


def test_paper_limit_waits_for_observed_executable_ask():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket())
    manager.observe_quote(record.correlation_id, 101, spot=24001)
    assert not record.ready_to_register
    manager.observe_quote(record.correlation_id, 99.95, spot=24001)
    assert record.ready_to_register
    assert record.filled_quantity == 130
    assert record.fill_price == 99.95
    assert record.mode == "PAPER"


@pytest.mark.parametrize("ask", [None, 0, -1, float("nan"), float("inf")])
def test_bad_quotes_never_fill(ask):
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket())
    manager.observe_quote(record.correlation_id, ask)
    assert record.filled_quantity == 0


def test_spot_invalidation_cancels_unfilled_entry():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket())
    manager.observe_quote(record.correlation_id, 99, spot=23989)
    assert record.status == "CANCELLED"
    assert not record.ready_to_register


def test_partial_fill_requires_protection_and_rejects_overfill_or_over_limit():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket())
    with pytest.raises(ValueError):
        manager.confirm_fill(record.correlation_id, 131, 99, True)
    with pytest.raises(ValueError):
        manager.confirm_fill(record.correlation_id, 65, 101, True)
    manager.confirm_fill(record.correlation_id, 65, 99, False)
    assert not record.ready_to_register
    manager.confirm_fill(record.correlation_id, 65, 99, True)
    assert record.ready_to_register
    assert record.filled_quantity == 65


def test_t1_books_whole_lots_and_runner_moves_to_spot_cost():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket(lots=3))
    manager.observe_quote(record.correlation_id, 100)
    manager.on_spot(record.correlation_id, 24020, option_bid=109)
    assert record.exited_quantity == 65
    assert record.active_spot_sl == 24000
    manager.on_spot(record.correlation_id, 24000, option_bid=98)
    assert record.exited_quantity == 195
    assert record.status == "CLOSED"
    assert record.exit_reason == "STOPPED_OUT_AT_BE"
    assert record.realized_pnl == pytest.approx(65 * 9 - 130 * 2)


def test_synthetic_exit_without_bid_is_not_a_fabricated_fill():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket())
    manager.observe_quote(record.correlation_id, 100)
    manager.on_spot(record.correlation_id, 23990)
    assert record.status == "EXIT_PENDING"
    assert record.exited_quantity == 0
    manager.observe_quote(record.correlation_id, 89, bid=88)
    assert record.status == "CLOSED"
    assert record.realized_pnl == -12 * 130


def test_bearish_stop_uses_underlying_direction():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket(option_type="PE", synthetic_spot_sl=24010,
                                            spot_target_1=23980, spot_target_2=23960))
    manager.observe_quote(record.correlation_id, 100)
    manager.on_spot(record.correlation_id, 23980, option_bid=110)
    assert record.active_spot_sl == 24000
    manager.on_spot(record.correlation_id, 24000, option_bid=99)
    assert record.status == "CLOSED"


class FakeDhan:
    def __init__(self):
        self.submissions = []
        self.book = []
        self.raise_after_submit = False

    def place_super_order(self, **payload):
        self.submissions.append(payload)
        if self.raise_after_submit:
            raise TimeoutError("ambiguous response")
        return {"status": "success", "data": {"orderId": str(len(self.submissions)),
                                                "orderStatus": "PENDING"}}

    def get_super_order_list(self):
        return {"status": "success", "data": self.book}

    def modify_super_order(self, **payload):
        return {"status": "success", "data": {"orderStatus": "TRANSIT"}}


def test_live_requires_explicit_opt_in_and_durable_journal(tmp_path):
    adapter = DhanSuperOrderAdapter(FakeDhan())
    with pytest.raises(ValueError):
        OrderManager(broker=adapter)
    with pytest.raises(ValueError):
        OrderManager(broker=adapter, allow_live=True)


def recovered_live_manager(tmp_path, sdk, lots=1):
    """Recover an existing journal without permitting new live submissions."""
    b = bracket(lots=lots)
    key = hashlib.sha256(b.signal_id.encode()).hexdigest()[:24]
    record = OrderRecord(key, b, mode="LIVE", active_spot_sl=b.synthetic_spot_sl,
                         slices=[{"quantity": lots * 65, "target": 110, "tag": key + "-0",
                                  "order_id": "1", "attempted": True}])
    path = tmp_path / "orders.json"
    path.write_text(json.dumps([asdict(record)]))
    manager = OrderManager(DhanSuperOrderAdapter(sdk), path, allow_live=True)
    return manager, manager.records[key]


def test_low_level_adapter_submits_atomic_limit_with_both_protective_legs():
    sdk = FakeDhan()
    order_id = DhanSuperOrderAdapter(sdk).submit(bracket(), 65, 110, "test-correlation")
    assert order_id == "1"
    payload = sdk.submissions[0]
    assert payload["quantity"] == 65
    assert payload["targetPrice"] == 110
    assert payload["order_type"] == "LIMIT"
    assert payload["stopLossPrice"] == 90
    assert payload["tag"] == "test-correlation"


def test_existing_ambiguous_journal_never_resubmits_after_restart(tmp_path):
    sdk = FakeDhan()
    manager, record = recovered_live_manager(tmp_path, sdk)
    assert record.status == "RECONCILIATION_REQUIRED"
    again = manager.dispatch_bracket(bracket(lots=1))
    assert again is record
    assert not sdk.submissions
    assert not again.ready_to_register


def test_reconciliation_checks_actual_fills_and_both_protective_legs(tmp_path):
    sdk = FakeDhan()
    manager, record = recovered_live_manager(tmp_path, sdk)
    sdk.book = [{"orderId": "1", "correlationId": record.slices[0]["tag"],
                 "orderStatus": "TRADED", "filledQty": 65, "averageTradedPrice": 99,
                 "legDetails": []}]
    manager.reconcile()
    assert record.filled_quantity == 65
    assert not record.ready_to_register
    sdk.book[0]["legDetails"] = [
        {"legName": name, "orderStatus": "PENDING", "remainingQuantity": 65}
        for name in ("TARGET_LEG", "STOP_LOSS_LEG")
    ]
    manager.reconcile()
    assert record.ready_to_register
    assert record.fill_price == 99


def test_triggered_broker_exit_never_counts_as_filled(tmp_path):
    sdk = FakeDhan()
    manager, record = recovered_live_manager(tmp_path, sdk)
    sdk.book = [{"orderId": "1", "orderStatus": "CLOSED", "filledQty": 65,
                 "averageTradedPrice": 100, "legDetails": [
                     {"legName": "TARGET_LEG", "orderStatus": "TRIGGERED", "triggeredQuantity": 65}
                 ]}]
    manager.reconcile()
    assert record.exited_quantity == 0
    assert record.status == "RECONCILIATION_REQUIRED"


def test_pending_entry_cancels_when_target_arrives_before_fill():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket())
    manager.observe_quote(record.correlation_id, 99, spot=24020)
    assert record.status == "CANCELLED"
    assert record.filled_quantity == 0


def test_expired_contract_and_invalid_geometry_are_rejected():
    manager = OrderManager()
    for index, invalid in enumerate((bracket(expiry_date="2000-01-01"),
                                     bracket(synthetic_spot_sl=24010),
                                     bracket(target_1=float("nan")))):
        record = manager.dispatch_bracket(invalid, correlation_id=str(index))
        assert record.status == "REJECTED"
        assert not record.ready_to_register


def test_one_lot_exits_entire_position_at_t1_observed_bid():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket(lots=1))
    manager.observe_quote(record.correlation_id, 100)
    manager.on_spot(record.correlation_id, 24020, option_bid=109)
    assert record.status == "CLOSED"
    assert record.exited_quantity == 65
    assert record.realized_pnl == 585


def test_paper_catastrophic_floor_requires_observed_bid():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket())
    manager.observe_quote(record.correlation_id, 100)
    manager.observe_quote(record.correlation_id, 89)
    assert record.status == "FILLED"
    manager.observe_quote(record.correlation_id, 89, bid=88)
    assert record.status == "CLOSED"
    assert record.realized_pnl == -1560


def test_changed_bracket_cannot_reuse_signal_correlation():
    manager = OrderManager()
    manager.dispatch_bracket(bracket())
    with pytest.raises(ValueError):
        manager.dispatch_bracket(bracket(lots=3))


def test_live_observed_quotes_never_synthesize_a_fill(tmp_path):
    manager = OrderManager(DhanSuperOrderAdapter(FakeDhan()), tmp_path / "orders.json", allow_live=True)
    record = manager.dispatch_bracket(bracket())
    manager.observe_quote(record.correlation_id, 99, spot=24001)
    assert record.filled_quantity == 0
    assert not record.ready_to_register


def test_reconciliation_failure_revokes_previously_confirmed_protection(tmp_path):
    sdk = FakeDhan()
    manager, record = recovered_live_manager(tmp_path, sdk)
    sdk.book = [{"orderId": "1", "filledQty": 65, "averageTradedPrice": 99,
                 "legDetails": [{"legName": name, "orderStatus": "PENDING", "remainingQuantity": 65}
                                for name in ("TARGET_LEG", "STOP_LOSS_LEG")]}]
    manager.reconcile()
    assert record.ready_to_register
    sdk.get_super_order_list = lambda: {"status": "failure", "data": []}
    with pytest.raises(RuntimeError):
        manager.reconcile()
    assert not record.ready_to_register
    assert record.status == "RECONCILIATION_REQUIRED"


def test_shipped_live_adapter_refuses_before_any_broker_submission(tmp_path):
    sdk = FakeDhan()
    manager = OrderManager(DhanSuperOrderAdapter(sdk), tmp_path / "orders.json", allow_live=True)
    record = manager.dispatch_bracket(bracket())
    assert record.status == "REJECTED"
    assert record.rejection_reason == "LIVE_RUNTIME_UNSUPPORTED"
    assert not sdk.submissions


def test_live_fill_cannot_be_injected_by_caller(tmp_path):
    manager = OrderManager(DhanSuperOrderAdapter(FakeDhan()), tmp_path / "orders.json", allow_live=True)
    record = manager.dispatch_bracket(bracket())
    with pytest.raises(ValueError, match="broker reconciliation"):
        manager.confirm_fill(record.correlation_id, 65, 99, True)
    assert not record.ready_to_register


def test_upfront_premium_targets_fill_without_a_spot_target_touch():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket())
    manager.observe_quote(record.correlation_id,100,spot=24001)
    manager.observe_quote(record.correlation_id,111,spot=24005,bid=110)
    assert record.exited_quantity == 65
    assert record.active_spot_sl == 24001
    manager.observe_quote(record.correlation_id,121,spot=24006,bid=120)
    assert record.status == "CLOSED"
    assert record.realized_pnl == 1950


def test_crossed_book_never_creates_a_paper_entry():
    manager = OrderManager()
    record = manager.dispatch_bracket(bracket())
    manager.observe_quote(record.correlation_id,99,spot=24001,bid=101)
    assert not record.ready_to_register


def test_dhan_adapter_submit_error_status():
    sdk = MagicMock()
    sdk.place_super_order.return_value = {"status": "error", "remarks": "Margin insufficient"}
    adapter = DhanSuperOrderAdapter(sdk)
    with pytest.raises(RuntimeError, match="Super-order submission not confirmed"):
        adapter.submit(bracket(), 65, 110, "tag-1")


def test_dhan_adapter_submit_missing_order_id():
    sdk = MagicMock()
    sdk.place_super_order.return_value = {"status": "success", "data": {}}
    adapter = DhanSuperOrderAdapter(sdk)
    with pytest.raises(RuntimeError, match="Super-order ID not confirmed"):
        adapter.submit(bracket(), 65, 110, "tag-1")


def test_journal_load_with_none_greeks(tmp_path):
    b = bracket()
    b = replace(b, greeks=None)
    key = "test-no-greeks"
    rec = OrderRecord(key, b, mode="PAPER")
    path = tmp_path / "orders.json"
    path.write_text(json.dumps([asdict(rec)]))
    mgr = OrderManager(journal_path=path)
    assert key in mgr.records
    assert mgr.records[key].bracket.greeks is None


def test_journal_mode_mismatch(tmp_path):
    rec = OrderRecord("corr-1", bracket(), mode="LIVE")
    path = tmp_path / "orders.json"
    path.write_text(json.dumps([asdict(rec)]))
    with pytest.raises(ValueError, match="Journal execution mode does not match manager"):
        OrderManager(journal_path=path)


def test_journal_load_live_terminal_record_not_reconciled(tmp_path):
    rec = OrderRecord("corr-closed", bracket(), mode="LIVE", status="CLOSED")
    path = tmp_path / "orders.json"
    path.write_text(json.dumps([asdict(rec)]))
    adapter = DhanSuperOrderAdapter(FakeDhan())
    mgr = OrderManager(broker=adapter, journal_path=path, allow_live=True)
    assert mgr.records["corr-closed"].status == "CLOSED"


def test_atomic_save_cleanup_on_exception(tmp_path, monkeypatch):
    path = tmp_path / "orders.json"
    mgr = OrderManager(journal_path=path)
    def failing_dump(*args, **kwargs):
        raise OSError("Disk full")
    monkeypatch.setattr(json, "dump", failing_dump)
    with pytest.raises(OSError, match="Disk full"):
        mgr.dispatch_bracket(bracket())
    assert list(tmp_path.glob(".orders-*")) == []


@pytest.mark.parametrize("invalid_expiry", ["not-a-date", None])
def test_bracket_malformed_expiry_date(invalid_expiry):
    manager = OrderManager()
    rec = manager.dispatch_bracket(bracket(expiry_date=invalid_expiry))
    assert rec.status == "REJECTED"


@pytest.mark.parametrize("terminal_status", ["CANCELLED", "REJECTED", "CLOSED"])
def test_confirm_fill_terminal_order_rejected(terminal_status):
    manager = OrderManager()
    rec = manager.dispatch_bracket(bracket())
    rec.status = terminal_status
    with pytest.raises(ValueError, match="Cannot fill terminal order"):
        manager._confirm_fill(rec.correlation_id, 65, 99.0, True)


def test_observe_quote_cancels_expired_submitted_order():
    manager = OrderManager()
    rec = manager.dispatch_bracket(bracket())
    rec.bracket = replace(rec.bracket, expiry_date="2020-01-01")
    manager.observe_quote(rec.correlation_id, 100.0)
    assert rec.status == "CANCELLED"


def test_paper_exit_zero_quantity_early_return():
    manager = OrderManager()
    rec = manager.dispatch_bracket(bracket())
    manager._paper_exit(rec, 0, 100.0, "TEST")
    assert rec.exit_reason == ""


@pytest.mark.parametrize("bad_spot", [0, -10, float("nan")])
def test_on_spot_non_positive_spot(bad_spot):
    manager = OrderManager()
    rec = manager.dispatch_bracket(bracket())
    assert manager.on_spot(rec.correlation_id, bad_spot) is rec
    assert rec.status == "SUBMITTED"


@pytest.mark.parametrize("term_status", ["CANCELLED", "REJECTED", "CLOSED"])
def test_on_spot_terminal_record(term_status):
    manager = OrderManager()
    rec = manager.dispatch_bracket(bracket())
    rec.status = term_status
    assert manager.on_spot(rec.correlation_id, 24000) is rec


def test_on_spot_live_mode_handling():
    manager = OrderManager()
    rec = manager.dispatch_bracket(bracket())
    rec.mode = "LIVE"
    rec.filled_quantity = 130
    rec.protection_confirmed = True
    rec.status = "FILLED"
    res1 = manager.on_spot(rec.correlation_id, 24005)
    assert res1.status == "FILLED"
    assert res1.protection_confirmed is True
    res2 = manager.on_spot(rec.correlation_id, 23985)
    assert res2.status == "RECONCILIATION_REQUIRED"
    assert res2.protection_confirmed is False


def test_on_spot_exit_pending_retry():
    manager = OrderManager()
    rec = manager.dispatch_bracket(bracket())
    manager.observe_quote(rec.correlation_id, 100.0)
    manager.on_spot(rec.correlation_id, 23985, option_bid=None)
    assert rec.status == "EXIT_PENDING"
    assert rec.pending_exit_quantity == 130
    manager.on_spot(rec.correlation_id, 24005, option_bid=None)
    assert rec.status == "EXIT_PENDING"
    manager.on_spot(rec.correlation_id, 24005, option_bid=95.0)
    assert rec.status == "CLOSED"
    assert rec.exited_quantity == 130


def test_on_spot_direct_t2_hit_on_large_candle():
    manager = OrderManager()
    rec = manager.dispatch_bracket(bracket(lots=2))
    manager.observe_quote(rec.correlation_id, 100.0)
    assert rec.filled_quantity == 130
    manager.on_spot(rec.correlation_id, 24050, option_bid=125.0)
    assert rec.status == "CLOSED"
    assert rec.exited_quantity == 130
    assert rec.exit_reason == "T2_HIT"


def test_on_spot_t2_runner_exit():
    manager = OrderManager()
    rec = manager.dispatch_bracket(bracket(lots=2))
    manager.observe_quote(rec.correlation_id, 100.0)
    manager.on_spot(rec.correlation_id, 24020, option_bid=110.0)
    assert rec.t1_hit is True
    assert rec.exited_quantity == 65
    assert rec.status == "FILLED"
    manager.on_spot(rec.correlation_id, 24040, option_bid=120.0)
    assert rec.status == "CLOSED"
    assert rec.exited_quantity == 130
    assert rec.exit_reason == "T2_HIT"


def test_reconcile_without_broker():
    manager = OrderManager()
    assert manager.reconcile() is None


def test_reconcile_skips_terminal_records(tmp_path):
    sdk = FakeDhan()
    manager, record = recovered_live_manager(tmp_path, sdk)
    record.status = "CLOSED"
    sdk.book = []
    manager.reconcile()
    assert record.status == "CLOSED"


def test_reconcile_non_unique_or_missing_slice_match(tmp_path):
    sdk = FakeDhan()
    manager, record = recovered_live_manager(tmp_path, sdk)
    sdk.book = []
    manager.reconcile()
    assert record.status == "RECONCILIATION_REQUIRED"
    assert record.protection_confirmed is False


def test_reconcile_corrupt_broker_fill_reporting(tmp_path):
    sdk = FakeDhan()
    manager, record = recovered_live_manager(tmp_path, sdk)
    sdk.book = [{"orderId": "1", "correlationId": record.slices[0]["tag"],
                 "orderStatus": "TRADED", "filledQty": 65, "averageTradedPrice": 105,
                 "legDetails": [{"legName": "TARGET_LEG", "orderStatus": "PENDING", "remainingQuantity": 65},
                                {"legName": "STOP_LOSS_LEG", "orderStatus": "PENDING", "remainingQuantity": 65}]}]
    manager.reconcile()
    assert record.status == "RECONCILIATION_REQUIRED"


def test_reconcile_unfilled_order_status_restored_to_submitted(tmp_path):
    sdk = FakeDhan()
    manager, record = recovered_live_manager(tmp_path, sdk)
    sdk.book = [{"orderId": "1", "correlationId": record.slices[0]["tag"],
                 "orderStatus": "PENDING", "filledQty": 0, "averageTradedPrice": 0,
                 "legDetails": [{"legName": "TARGET_LEG", "orderStatus": "PENDING", "remainingQuantity": 65},
                                {"legName": "STOP_LOSS_LEG", "orderStatus": "PENDING", "remainingQuantity": 65}]}]
    manager.reconcile()
    assert record.status == "SUBMITTED"
