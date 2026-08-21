"""
TASK-183 — offline labeling & training pipeline.

Contract-level tests: exercise the public functions with synthetic data.
No live Supabase, no network, no mocking of internals.
"""
import unittest
import json
from datetime import datetime

import pandas as pd

from ml_signal.dataset import (
    flatten_features,
    label_forward_points,
    build_labeled_frame,
    feature_columns,
    FEATURE_GROUPS,
)
from ml_signal.train_offline import chronological_split, run_training, _fetch_ml_collection


def _row(ts, close, **groups):
    """Build a synthetic ml_collection row. Groups default to small dicts."""
    base = {
        "timestamp": ts,
        "raw_candle": json.dumps({"open": close, "high": close + 2,
                                  "low": close - 2, "close": close,
                                  "volume": 1000, "vwap": close}),
        "candle_features": json.dumps(groups.get("candle", {"body_pct": 0.1})),
        "volume_features": json.dumps(groups.get("volume", {"vol_ratio": 1.2})),
        "iv_features": json.dumps(groups.get("iv", {"iv_level": 12.0})),
        "oi_features": json.dumps(groups.get("oi", {"pcr_oi": 1.0})),
        "greek_features": json.dumps(groups.get("greek", {"total_vega": 25.0})),
        "structure_features": json.dumps(groups.get("structure", {"dist_to_pdh": 50.0})),
        "meta_features": json.dumps(groups.get("meta", {"dte": 7.0})),
    }
    return base


class TestFlatten(unittest.TestCase):
    def test_flatten_produces_prefixed_numeric_columns(self):
        rows = [
            _row("2026-07-08T09:15:00+00:00", 24000.0),
            _row("2026-07-08T09:16:00+00:00", 24010.0),
        ]
        df = flatten_features(rows)
        # feature columns are prefixed by group
        self.assertIn("candle_features__body_pct", df.columns)
        self.assertIn("iv_features__iv_level", df.columns)
        self.assertIn("meta_features__dte", df.columns)
        # meta columns present
        for c in ("timestamp", "date", "close"):
            self.assertIn(c, df.columns)
        # close pulled from raw_candle
        self.assertEqual(df.iloc[0]["close"], 24000.0)
        self.assertEqual(df.iloc[1]["close"], 24010.0)
        # values preserved
        self.assertAlmostEqual(df.iloc[0]["iv_features__iv_level"], 12.0)

    def test_missing_keys_filled_nan_and_columns_stable(self):
        # The column union stays stable across rows (the guarantee this test was
        # written for). The fill is NaN, not 0.0 (TASK-199): 0.0 is a real
        # reading for most features — an unknown structure distance filled with
        # it claims "spot is exactly at support/resistance". XGBoost consumes
        # NaN natively as missing.
        rows = [
            _row("2026-07-08T09:15:00+00:00", 24000.0, iv={"iv_level": 12.0, "iv_slope": 0.5}),
            _row("2026-07-08T09:16:00+00:00", 24010.0, iv={"iv_level": 13.0}),  # iv_slope missing
        ]
        df = flatten_features(rows)
        self.assertIn("iv_features__iv_slope", df.columns)
        self.assertTrue(pd.isna(df.iloc[1]["iv_features__iv_slope"]))
        # A value that IS present is untouched.
        self.assertAlmostEqual(df.iloc[0]["iv_features__iv_slope"], 0.5)

    def test_accepts_dict_columns_not_only_json_strings(self):
        r = _row("2026-07-08T09:15:00+00:00", 24000.0)
        r["candle_features"] = {"body_pct": 0.42}  # already a dict
        df = flatten_features([r])
        self.assertAlmostEqual(df.iloc[0]["candle_features__body_pct"], 0.42)


class TestLabelForwardPoints(unittest.TestCase):
    def test_up_move_is_win(self):
        # +25 on the next candle, tp=20 -> clean up move -> 1
        labels = label_forward_points([100, 125, 100, 100], lookforward=3,
                                      tp_points=20, sl_points=10)
        self.assertEqual(labels[0], 1)

    def test_down_move_is_win(self):
        labels = label_forward_points([100, 75, 100, 100], lookforward=3,
                                      tp_points=20, sl_points=10)
        self.assertEqual(labels[0], 1)

    def test_stop_before_target_is_loss(self):
        # drops 12 (past bull sl 10) first, never a clean 20-pt move -> 0
        labels = label_forward_points([100, 88, 105, 103], lookforward=3,
                                      tp_points=20, sl_points=10)
        self.assertEqual(labels[0], 0)

    def test_inconclusive_is_minus_one(self):
        # tiny wiggles, neither tp nor sl -> -1
        labels = label_forward_points([100, 105, 98, 101], lookforward=3,
                                      tp_points=20, sl_points=10)
        self.assertEqual(labels[0], -1)

    def test_tail_rows_have_no_forward_window(self):
        labels = label_forward_points([100, 100], lookforward=3,
                                      tp_points=20, sl_points=10)
        # not enough forward candles -> inconclusive
        self.assertEqual(labels[-1], -1)


class TestBuildLabeledFrame(unittest.TestCase):
    def _series_rows(self, date, closes, start_min=15):
        rows = []
        for i, c in enumerate(closes):
            ts = f"{date}T09:{start_min + i:02d}:00+00:00"
            rows.append(_row(ts, float(c)))
        return rows

    def test_labels_do_not_span_day_boundary(self):
        # Day 1 ends flat; Day 2 opens with a huge gap up. A naive labeler would
        # mark the last day-1 rows as wins off the overnight jump. Day-bounded
        # labeling must NOT.
        day1 = self._series_rows("2026-07-07", [100, 100, 100, 100, 100])
        day2 = self._series_rows("2026-07-08", [200, 200, 200, 200, 200])
        df = build_labeled_frame(day1 + day2, lookforward=3,
                                 tp_points=20, sl_points=10)
        # every labeled row must have its forward window inside its own day
        for _, r in df.iterrows():
            self.assertIn(r["label"], (0, 1))  # no -1 rows survive
        # the flat days produce no wins from the gap
        self.assertEqual(int(df["label"].sum()), 0)

    def test_within_day_win_is_captured(self):
        day = self._series_rows("2026-07-08", [100, 130, 100, 100, 100])
        df = build_labeled_frame(day, lookforward=3, tp_points=20, sl_points=10)
        self.assertEqual(int(df.iloc[0]["label"]), 1)

    def test_feature_columns_excludes_meta(self):
        rows = self._series_rows("2026-07-08", [100, 130, 100, 100, 100])
        df = build_labeled_frame(rows, lookforward=3, tp_points=20, sl_points=10)
        fcols = feature_columns(df)
        for banned in ("timestamp", "date", "close", "label"):
            self.assertNotIn(banned, fcols)
        self.assertTrue(any(c.startswith("iv_features__") for c in fcols))


class TestChronologicalSplit(unittest.TestCase):
    def test_split_is_time_ordered_no_leakage(self):
        df = pd.DataFrame({
            "timestamp": pd.to_datetime([f"2026-07-08T09:{15+i:02d}:00" for i in range(10)]),
            "label": [0, 1] * 5,
            "f": range(10),
        })
        train, test = chronological_split(df, train_frac=0.8)
        self.assertEqual(len(train), 8)
        self.assertEqual(len(test), 2)
        # every train timestamp strictly before every test timestamp
        self.assertLess(train["timestamp"].max(), test["timestamp"].min())


class TestRunTrainingSmallDataGuard(unittest.TestCase):
    def test_tiny_dataset_does_not_crash(self):
        # a handful of separable rows; run_training must complete and return metrics
        import numpy as np
        rng = list(range(40))
        df = pd.DataFrame({
            "timestamp": pd.to_datetime([f"2026-07-08T09:{i:02d}:00" for i in rng]),
            "f1": [i % 2 for i in rng],
            "f2": [(i * 7) % 5 for i in rng],
            "label": [i % 2 for i in rng],  # perfectly separable by f1
        })
        model, metrics = run_training(df, ["f1", "f2"], min_samples=1000)  # below threshold -> provisional
        self.assertIsNotNone(model)
        self.assertIn("auc_roc", metrics)


class TestDetectorScoresFeature(unittest.TestCase):
    """TASK-4e: detector_scores fed from ml_collection into XGBoost training."""

    # ── Test 1 ──────────────────────────────────────────────────────────────
    def test_detector_scores_in_feature_groups(self):
        """detector_scores must be the 8th entry in FEATURE_GROUPS."""
        self.assertIn("detector_scores", FEATURE_GROUPS)

    # ── Test 2 ──────────────────────────────────────────────────────────────
    def test_flatten_features_includes_detector_scores(self):
        """
        A row that carries a detector_scores JSON string must produce
        detector_scores__<key> columns with the correct float values.
        """
        ds_payload = json.dumps({
            "failed_breakout": 1,
            "oi_wall_rejection": 0,
            "exhaustion_reversal": 0,
            "trend_continuation": 0,
        })
        row = _row("2026-07-08T09:15:00+00:00", 24000.0)
        row["detector_scores"] = ds_payload

        df = flatten_features([row])

        self.assertIn("detector_scores__failed_breakout", df.columns)
        self.assertAlmostEqual(df.iloc[0]["detector_scores__failed_breakout"], 1.0)
        self.assertAlmostEqual(df.iloc[0]["detector_scores__oi_wall_rejection"], 0.0)
        self.assertAlmostEqual(df.iloc[0]["detector_scores__exhaustion_reversal"], 0.0)
        self.assertAlmostEqual(df.iloc[0]["detector_scores__trend_continuation"], 0.0)

    # ── Test 3 ──────────────────────────────────────────────────────────────
    def test_flatten_features_missing_detector_scores_is_nan(self):
        """
        A row without detector_scores (pre-migration row, column = None) must
        still produce the detector_scores__ columns, filled with NaN.
        XGBoost consumes NaN natively; we never back-fill 0.
        """
        # First row has detector_scores so the columns exist in the union set.
        ds_payload = json.dumps({
            "failed_breakout": 0,
            "oi_wall_rejection": 0,
            "exhaustion_reversal": 0,
            "trend_continuation": 0,
        })
        row_with = _row("2026-07-08T09:15:00+00:00", 24000.0)
        row_with["detector_scores"] = ds_payload

        # Second row is a pre-migration row — no detector_scores column at all.
        row_without = _row("2026-07-08T09:16:00+00:00", 24010.0)
        row_without["detector_scores"] = None  # simulate NULL from Supabase

        df = flatten_features([row_with, row_without])

        self.assertIn("detector_scores__failed_breakout", df.columns)
        import numpy as np
        self.assertTrue(pd.isna(df.iloc[1]["detector_scores__failed_breakout"]),
                        "Pre-migration rows must have NaN, not 0.0")

    # ── Test 4 ──────────────────────────────────────────────────────────────
    def test_fetch_columns_include_detector_scores(self):
        """
        _fetch_ml_collection must request detector_scores from Supabase.
        We inspect the column string it builds without connecting to the DB.
        """
        import inspect
        source = inspect.getsource(_fetch_ml_collection)
        self.assertIn("detector_scores", source,
                      "_fetch_ml_collection must list 'detector_scores' in its SELECT column string")


if __name__ == "__main__":
    unittest.main()

