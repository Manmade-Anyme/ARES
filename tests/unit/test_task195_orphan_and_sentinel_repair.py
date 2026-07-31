"""TASK-195 — repair, rather than delete, the two data defects TASK-194 surfaced.

Both were candidates for deletion. Both are recoverable, and deleting either
would have destroyed materially more than it removed:

* 36 trade_analytics rows carry no signal_id. They still hold full entry/exit
  data and +728.2 net points, 8 of them are still OPEN, and 17 are
  OI_WALL_REJECTION — the detector with the thinnest joinable sample. 29 of the
  36 match a signal on (entry_timestamp, setup_type) with a median delta of 0s.

* 5,226 ml_collection rows carry the literal 100.0 sentinel in
  structure_features. NOT ONE has all four fields poisoned — every row has 1-3
  bad fields and 29-32 good ones. Deleting the rows to remove at most two fields
  would have cut the table from 9,102 to 3,876 and taken the whole OI-clean and
  IV-clean windows with it.

Guards mirror the ones review added to TASK-194's repair: claim each signal once,
corroborate rather than assume, report contention instead of overwriting.
"""

import json
import unittest
from unittest.mock import MagicMock

from tests.unit.test_task194_ml_labels_and_oi_distribution import _FakeSupabase


def _mod():
    from ml_signal import backfill_labels
    return backfill_labels


class TestOrphanTradeRepair(unittest.TestCase):

    def test_orphan_is_matched_on_entry_timestamp_and_setup(self):
        rows = {
            "trade_analytics": [
                {"id": "t1", "signal_id": None, "setup_type": "OI_WALL_REJECTION",
                 "entry_timestamp": "2026-07-23T03:49:20+00:00"},
            ],
            "ares_signals": [
                {"id": 243, "setup_type": "OI_WALL_REJECTION",
                 "timestamp": "2026-07-23T03:49:19+00:00",
                 "created_at": "2026-07-23T03:49:19+00:00"},
            ],
        }
        sb = _FakeSupabase(rows)
        fixed = _mod().repair_orphan_trades(sb, apply=True)
        self.assertEqual(fixed, 1)
        self.assertEqual(rows["trade_analytics"][0]["signal_id"], 243)

    def test_open_trades_are_repaired_too(self):
        """signal_id is independent of result_state — an OPEN trade still links."""
        rows = {
            "trade_analytics": [
                {"id": "t1", "signal_id": None, "setup_type": "EXHAUSTION_REVERSAL",
                 "entry_timestamp": "2026-07-30T05:00:00+00:00", "result_state": "OPEN"},
            ],
            "ares_signals": [
                {"id": 271, "setup_type": "EXHAUSTION_REVERSAL",
                 "timestamp": "2026-07-30T05:00:00+00:00",
                 "created_at": "2026-07-30T05:00:00+00:00"},
            ],
        }
        sb = _FakeSupabase(rows)
        self.assertEqual(_mod().repair_orphan_trades(sb, apply=True), 1)

    def test_a_signal_already_used_by_another_trade_is_not_stolen(self):
        rows = {
            "trade_analytics": [
                {"id": "t1", "signal_id": 243, "setup_type": "OI_WALL_REJECTION",
                 "entry_timestamp": "2026-07-23T03:49:20+00:00"},
                {"id": "t2", "signal_id": None, "setup_type": "OI_WALL_REJECTION",
                 "entry_timestamp": "2026-07-23T03:49:21+00:00"},
            ],
            "ares_signals": [
                {"id": 243, "setup_type": "OI_WALL_REJECTION",
                 "timestamp": "2026-07-23T03:49:19+00:00",
                 "created_at": "2026-07-23T03:49:19+00:00"},
            ],
        }
        sb = _FakeSupabase(rows)
        self.assertEqual(_mod().repair_orphan_trades(sb, apply=True), 0)
        self.assertIsNone(rows["trade_analytics"][1]["signal_id"])

    def test_far_from_any_signal_is_left_alone(self):
        rows = {
            "trade_analytics": [
                {"id": "t1", "signal_id": None, "setup_type": "FAILED_BREAKOUT",
                 "entry_timestamp": "2026-07-23T09:00:00+00:00"},
            ],
            "ares_signals": [
                {"id": 250, "setup_type": "FAILED_BREAKOUT",
                 "timestamp": "2026-07-23T03:00:00+00:00",
                 "created_at": "2026-07-23T03:00:00+00:00"},
            ],
        }
        sb = _FakeSupabase(rows)
        self.assertEqual(_mod().repair_orphan_trades(sb, apply=True), 0)
        self.assertIsNone(rows["trade_analytics"][0]["signal_id"])

    def test_setup_type_must_agree(self):
        rows = {
            "trade_analytics": [
                {"id": "t1", "signal_id": None, "setup_type": "FAILED_BREAKOUT",
                 "entry_timestamp": "2026-07-23T03:49:20+00:00"},
            ],
            "ares_signals": [
                {"id": 243, "setup_type": "OI_WALL_REJECTION",
                 "timestamp": "2026-07-23T03:49:19+00:00",
                 "created_at": "2026-07-23T03:49:19+00:00"},
            ],
        }
        sb = _FakeSupabase(rows)
        self.assertEqual(_mod().repair_orphan_trades(sb, apply=True), 0)


class TestSentinelRepair(unittest.TestCase):

    @staticmethod
    def _row(rid, **kv):
        return {"id": rid, "structure_features": json.dumps(kv)}

    def test_only_the_exact_sentinel_fields_are_nulled(self):
        rows = {"ml_collection": [self._row(
            1,
            dist_to_nearest_resistance=100.0,   # sentinel
            dist_to_nearest_support=37.5,       # real
            dist_to_pdh=100.0,                  # sentinel
            dist_to_pdl=-212.4,                 # real
        )]}
        sb = _FakeSupabase(rows)
        n = _mod().repair_structure_sentinel(sb, apply=True)
        self.assertEqual(n, 1)
        out = json.loads(rows["ml_collection"][0]["structure_features"])
        self.assertIsNone(out["dist_to_nearest_resistance"])
        self.assertIsNone(out["dist_to_pdh"])
        self.assertEqual(out["dist_to_nearest_support"], 37.5)
        self.assertEqual(out["dist_to_pdl"], -212.4)

    def test_rows_are_never_deleted_and_other_keys_survive(self):
        rows = {"ml_collection": [self._row(
            1, dist_to_nearest_resistance=100.0, some_future_key=1.23,
        )]}
        sb = _FakeSupabase(rows)
        _mod().repair_structure_sentinel(sb, apply=True)
        self.assertEqual(len(rows["ml_collection"]), 1, "repair must not delete rows")
        out = json.loads(rows["ml_collection"][0]["structure_features"])
        self.assertEqual(out["some_future_key"], 1.23)

    def test_clean_rows_are_not_rewritten(self):
        rows = {"ml_collection": [self._row(1, dist_to_nearest_resistance=42.0)]}
        sb = _FakeSupabase(rows)
        self.assertEqual(_mod().repair_structure_sentinel(sb, apply=True), 0)
        self.assertFalse(sb.updates, "a clean row must not be written at all")

    def test_a_near_100_value_is_not_treated_as_a_sentinel(self):
        """100.0000001 is a real distance; only exact equality is the marker."""
        rows = {"ml_collection": [self._row(1, dist_to_nearest_resistance=100.0000001)]}
        sb = _FakeSupabase(rows)
        self.assertEqual(_mod().repair_structure_sentinel(sb, apply=True), 0)

    def test_dry_run_writes_nothing(self):
        rows = {"ml_collection": [self._row(1, dist_to_nearest_resistance=100.0)]}
        sb = _FakeSupabase(rows)
        n = _mod().repair_structure_sentinel(sb, apply=False)
        self.assertEqual(n, 1, "dry run must still report what it would fix")
        self.assertFalse(sb.updates)


if __name__ == "__main__":
    unittest.main()
