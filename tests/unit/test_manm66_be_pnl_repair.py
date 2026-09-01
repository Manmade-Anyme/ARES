"""MANM-66 regression tests for historical BE-after-T1 PnL repair."""

import unittest
from contextlib import redirect_stdout
from io import StringIO

from tests.unit.test_task194_ml_labels_and_oi_distribution import _FakeSupabase


class TestBreakevenAfterT1Repair(unittest.TestCase):
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
                {"id": 102, "target_1": 23950.0},
                {"id": 103, "target_1": 24020.0},
            ],
            "ml_collection": [
                {"id": 1, "signal_id": "101", "trade_pnl": 0.0},
                {"id": 2, "signal_id": "102", "trade_pnl": 0.0},
                {"id": 3, "signal_id": "103", "trade_pnl": 12.0},
                {"id": 4, "signal_id": "999", "trade_pnl": 0.0},
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


if __name__ == "__main__":
    unittest.main()
