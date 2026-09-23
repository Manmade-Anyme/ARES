"""MANM-66 regression tests for historical BE-after-T1 PnL repair."""

import unittest
from contextlib import redirect_stdout
from io import StringIO

from tests.unit.test_task194_ml_labels_and_oi_distribution import _FakeSupabase


class TestBreakevenAfterT1Repair(unittest.TestCase):

    def test_repair_stuck_open_trades(self):
        from ml_signal import backfill_labels
        rows = {
            "trade_analytics": [
                {
                    "id": "t-open",
                    "result_state": "OPEN",
                    "entry_price": 24000.0,
                    "direction": "BULLISH"
                }
            ],
            "active_trades": [
                {
                    "id": "t-open",
                    "exit_price": 24100.0,
                    "exit_type": "T1_HIT",
                    "exit_timestamp": "2026-08-25T08:42:00+00:00",
                    "pnl_points_override": None
                }
            ]
        }
        sb = _FakeSupabase(rows)
        repaired = backfill_labels.repair_stuck_open_trades(sb, apply=True)
        self.assertEqual(repaired, 1)
        self.assertEqual(rows["trade_analytics"][0]["result_state"], "T1_HIT")
        self.assertEqual(rows["trade_analytics"][0]["pnl_points"], 100.0)
        self.assertEqual(rows["trade_analytics"][0]["score"], 1)

    def test_repair_recreates_missing_analytics_row_and_labels_ml(self):
        from ml_signal import backfill_labels

        rows = {
            "trade_analytics": [],
            "active_trades": [{
                "id": "t-missing",
                "signal_id": "0042",
                "setup_type": "FAILED_BREAKOUT",
                "direction": "BULLISH",
                "entry_price": 24000.0,
                "created_at": "2026-08-25T08:40:00+00:00",
                "state": "CLOSED",
                "exit_price": 24100.0,
                "exit_type": "T2_HIT",
                "exit_timestamp": "2026-08-25T08:42:00+00:00",
                "pnl_points_override": None,
            }],
            "ml_collection": [{
                "id": 1,
                "signal_id": "0042",
                "signal_setup_type": "FAILED_BREAKOUT",
                "timestamp": "2026-08-25T08:40:01+00:00",
                "trade_id": None,
            }],
        }
        sb = _FakeSupabase(rows)

        self.assertEqual(backfill_labels.repair_stuck_open_trades(sb, apply=True), 1)
        self.assertEqual(backfill_labels.backfill_labels(sb, apply=True), 1)

        analytics = rows["trade_analytics"][0]
        self.assertEqual(analytics["id"], "t-missing")
        self.assertEqual(analytics["result_state"], "T2_HIT")
        self.assertEqual(analytics["score"], 2)
        self.assertEqual(rows["ml_collection"][0]["trade_id"], "t-missing")
        self.assertEqual(rows["ml_collection"][0]["trade_score"], 2)

    def test_repair_recreates_missing_analytics_row_and_preserves_anomaly(self):
        from ml_signal import backfill_labels

        rows = {
            "trade_analytics": [],
            "active_trades": [{
                "id": "t-missing-anomaly",
                "signal_id": "0043",
                "setup_type": "FAILED_BREAKOUT",
                "direction": "BEARISH",
                "entry_price": 24000.0,
                "created_at": "2026-08-25T08:40:00+00:00",
                "state": "CLOSED",
                "exit_price": 24100.0,
                "exit_type": "SL_HIT",
                "exit_timestamp": "2026-08-25T08:35:00+00:00",
                "pnl_points_override": None,
                "time_metrics_excluded": True,
            }],
        }
        sb = _FakeSupabase(rows)

        self.assertEqual(backfill_labels.repair_stuck_open_trades(sb, apply=True), 1)

        analytics = rows["trade_analytics"][0]
        self.assertEqual(analytics["id"], "t-missing-anomaly")
        self.assertTrue(analytics.get("time_metrics_excluded"))
        self.assertIn("anomaly", analytics.get("market_context", {}))
        self.assertEqual(analytics["market_context"]["anomaly"]["type"], "unrecoverable_legacy_row")

    def test_repair_uses_persisted_entry_timestamp_before_created_at(self):
        from ml_signal import backfill_labels

        rows = {
            "trade_analytics": [],
            "active_trades": [{
                "id": "t-delayed-persistence",
                "signal_id": "0045",
                "setup_type": "FAILED_BREAKOUT",
                "direction": "BULLISH",
                "entry_price": 24000.0,
                "entry_timestamp": "2026-08-25T08:30:00+00:00",
                "created_at": "2026-08-25T08:40:00+00:00",
                "state": "CLOSED",
                "exit_price": 24100.0,
                "exit_type": "T2_HIT",
                "exit_timestamp": "2026-08-25T08:35:00+00:00",
                "pnl_points_override": None,
                "time_metrics_excluded": False,
            }],
        }
        sb = _FakeSupabase(rows)

        self.assertEqual(backfill_labels.repair_stuck_open_trades(sb, apply=True), 1)

        analytics = rows["trade_analytics"][0]
        self.assertEqual(analytics["entry_timestamp"], "2026-08-25T08:30:00+00:00")
        self.assertFalse(analytics.get("time_metrics_excluded", False))

    def test_repair_dynamically_calculates_anomaly(self):
        from ml_signal import backfill_labels

        rows = {
            "trade_analytics": [],
            "active_trades": [{
                "id": "t-missing-anomaly-dynamic",
                "signal_id": "0044",
                "setup_type": "FAILED_BREAKOUT",
                "direction": "BEARISH",
                "entry_price": 24000.0,
                "created_at": "2026-08-25T08:40:00+00:00",
                "state": "CLOSED",
                "exit_price": 24100.0,
                "exit_type": "SL_HIT",
                "exit_timestamp": "2026-08-25T08:35:00+00:00",
                "pnl_points_override": None,
                "time_metrics_excluded": False,
            }],
        }
        sb = _FakeSupabase(rows)

        self.assertEqual(backfill_labels.repair_stuck_open_trades(sb, apply=True), 1)

        analytics = rows["trade_analytics"][0]
        self.assertEqual(analytics["id"], "t-missing-anomaly-dynamic")
        self.assertTrue(analytics.get("time_metrics_excluded"))
        self.assertIn("anomaly", analytics.get("market_context", {}))
        self.assertEqual(analytics["market_context"]["anomaly"]["type"], "unrecoverable_legacy_row")

    @staticmethod
    def _rows():
        return {
            "trade_analytics": [
                {
                    "id": "t-active",
                    "signal_id": 101,
                    "result_state": "STOPPED_OUT_AT_BE",
                    "pnl_points": 0.0,
                    "entry_price": 24000.0,
                    "direction": "BULLISH",
                    "entry_timestamp": "2026-08-21T08:42:00+00:00",
                    "exit_price": 24000.0,
                },
                {
                    "id": "t-fallback",
                    "signal_id": 102,
                    "result_state": "STOPPED_OUT_AT_BE",
                    "pnl_points": 0.0,
                    "entry_price": 24000.0,
                    "direction": "BEARISH",
                    "setup_type": "TEST_SETUP",
                    "entry_timestamp": "2026-08-22T08:42:00+00:00",
                    "exit_price": 24000.0,
                },
                {
                    "id": "t-nonzero",
                    "signal_id": 103,
                    "result_state": "STOPPED_OUT_AT_BE",
                    "pnl_points": 12.0,
                    "entry_price": 24000.0,
                    "direction": "BULLISH",
                    "entry_timestamp": "2026-08-23T08:42:00+00:00",
                    "exit_price": 24000.0,
                },
                {
                    "id": "t-unrepairable",
                    "signal_id": 104,
                    "result_state": "STOPPED_OUT_AT_BE",
                    "pnl_points": 0.0,
                    "entry_price": 24000.0,
                    "direction": "BULLISH",
                    "entry_timestamp": "2026-08-24T08:42:00+00:00",
                    "exit_price": 24000.0,
                },
            ],
            "active_trades": [
                # The active-trade value must win over the signal fallback.
                {"id": "t-active", "target_1": 24040.0},
            ],
            "ares_signals": [
                {"id": 101, "target_1": 24060.0},
                {"id": 102, "target_1": 23950.0, "timestamp": "2026-08-22T08:42:00+00:00", "setup_type": "TEST_SETUP"},
                {"id": 103, "target_1": 24020.0},
            ],
            "ml_collection": [
                {"id": 1, "trade_id": "t-active", "signal_id": "101", "trade_pnl": 0.0},
                {"id": 2, "trade_id": "t-fallback", "signal_id": "102", "trade_pnl": 0.0},
                {"id": 3, "trade_id": "t-nonzero", "signal_id": "103", "trade_pnl": 12.0},
                {"id": 4, "trade_id": "t-missing", "signal_id": "999", "trade_pnl": 0.0},
            ],
        }

    def test_apply_repairs_zero_rows_and_syncs_exact_signal_ids(self):
        from ml_signal import backfill_labels

        rows = self._rows()
        sb = _FakeSupabase(rows)

        repaired = backfill_labels.repair_be_after_t1(sb, apply=True)

        self.assertEqual(repaired, 2)
        self.assertEqual(rows["trade_analytics"][0]["pnl_points"], 40.0)
        self.assertEqual(rows["trade_analytics"][1]["pnl_points"], 50.0)
        self.assertEqual(rows["trade_analytics"][2]["pnl_points"], 12.0)
        self.assertEqual(rows["trade_analytics"][3]["pnl_points"], 0.0)
        self.assertEqual(rows["trade_analytics"][0]["exit_price"], 24000.0)
        self.assertEqual(rows["ml_collection"][0]["trade_pnl"], 40.0)
        self.assertEqual(rows["ml_collection"][1]["trade_pnl"], 50.0)
        self.assertEqual(rows["ml_collection"][2]["trade_pnl"], 12.0)
        self.assertEqual(rows["ml_collection"][3]["trade_pnl"], 0.0)

    def test_dry_run_reports_scope_and_writes_nothing(self):
        from ml_signal import backfill_labels

        rows = self._rows()
        sb = _FakeSupabase(rows)
        output = StringIO()
        with redirect_stdout(output):
            repaired = backfill_labels.repair_be_after_t1(sb, apply=False)

        self.assertEqual(repaired, 2)
        self.assertFalse(sb.updates)
        self.assertEqual(rows["trade_analytics"][0]["pnl_points"], 0.0)
        report = output.getvalue()
        self.assertIn("STOPPED_OUT_AT_BE", report)
        self.assertIn("already nonzero", report)
        self.assertIn("total points delta", report)
        self.assertIn("unrepairable", report)

    def test_dry_run_simulates_orphan_signal_recovery_before_be_repair(self):
        from ml_signal import backfill_labels

        rows = {
            "trade_analytics": [{
                "id": "t-orphan",
                "signal_id": None,
                "setup_type": "OI_WALL_REJECTION",
                "result_state": "STOPPED_OUT_AT_BE",
                "pnl_points": 0.0,
                "entry_price": 24000.0,
                "direction": "BULLISH",
                "entry_timestamp": "2026-08-25T08:42:00+00:00",
                "exit_price": 24000.0,
            }],
            "active_trades": [],
            "ares_signals": [{
                "id": 205,
                "setup_type": "OI_WALL_REJECTION",
                "target_1": 24045.0,
                "timestamp": "2026-08-25T08:42:00+00:00",
                "created_at": "2026-08-25T08:42:00+00:00",
            }],
            "ml_collection": [{"id": 1, "signal_id": "205", "trade_pnl": 0.0}],
        }
        sb = _FakeSupabase(rows)
        prospective_links = {}

        self.assertEqual(
            backfill_labels.repair_orphan_trades(
                sb, apply=False, prospective_links=prospective_links
            ),
            1,
        )
        self.assertEqual(prospective_links, {"t-orphan": 205})
        self.assertEqual(
            backfill_labels.repair_be_after_t1(
                sb, apply=False, prospective_signal_ids=prospective_links
            ),
            1,
        )
        self.assertFalse(sb.updates)

    def test_legacy_fallback_projects_timestamp_and_setup_fields(self):
        from ml_signal import backfill_labels

        rows = self._rows()
        rows["active_trades"] = []
        rows["trade_analytics"] = [rows["trade_analytics"][1]]
        rows["ml_collection"] = [rows["ml_collection"][1]]
        sb = _FakeSupabase(rows)

        repaired = backfill_labels.repair_be_after_t1(sb, apply=False)

        self.assertEqual(repaired, 1)

    def test_legacy_fallback_normalizes_naive_ist_against_aware_utc(self):
        from ml_signal import backfill_labels

        rows = self._rows()
        rows["active_trades"] = []
        rows["trade_analytics"] = [rows["trade_analytics"][1]]
        rows["ml_collection"] = [rows["ml_collection"][1]]
        rows["ares_signals"][1]["timestamp"] = "2026-08-22T14:12:00"
        sb = _FakeSupabase(rows)

        repaired = backfill_labels.repair_be_after_t1(sb, apply=False)

        self.assertEqual(repaired, 1)


if __name__ == "__main__":
    unittest.main()
