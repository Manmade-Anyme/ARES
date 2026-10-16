"""Execution contracts; fake SDK responses never contact an account."""
from dataclasses import asdict, replace
import hashlib
import json

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
