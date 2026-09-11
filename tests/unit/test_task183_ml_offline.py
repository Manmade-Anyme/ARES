"""
TASK-183 — offline labeling & training pipeline.

Contract-level tests: exercise the public functions with synthetic data.
No live Supabase or network; mocks are limited to optional/external boundaries
and targeted failure boundaries needed to validate graceful degradation.
"""
import unittest
import json
import os
import sys
import tempfile
import types
import builtins
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import mock_open, patch

import numpy as np
import pandas as pd

from ml_signal.dataset import (
    flatten_features,
    label_forward_points,
    build_labeled_frame,
    feature_columns,
    FEATURE_GROUPS,
)
from ml_signal.train_offline import (
    chronological_split,
    run_training,
    _compute_shap,
    _fetch_ml_collection,
    _native_shap_values,
    _offline_report_paths,
    _print_shap_summary,
    _sharpe_metrics,
    _shap_metrics,
)
from ml_signal.config import MLConfig


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


class TestSharpeMetrics(unittest.TestCase):
    def test_annualizes_daily_pnl_without_becoming_a_feature(self):
        df = pd.DataFrame({
            "timestamp": [
                "2026-08-03T04:00:00+00:00",  # 09:30 IST
                "2026-08-03T05:00:00+00:00",  # same day
                "2026-08-04T04:00:00+00:00",
                "2026-08-05T04:00:00+00:00",
            ],
            "pnl_points": [10.0, -2.0, -4.0, 8.0],
            "label": [1, 0, 0, 1],
            "alpha": [1.0, 2.0, 3.0, 4.0],
        })
        metrics = _sharpe_metrics(df)
        # Daily P&L is [8, -4, 8], so mean/std is 0.57735.
        self.assertEqual(metrics["sharpe_status"], "computed")
        self.assertEqual(metrics["sharpe_days"], 3)
        self.assertEqual(metrics["sharpe_trades"], 4)
        self.assertEqual(metrics["sharpe_day_basis"], "active_trading_days")
        self.assertIn("active-trading-day", metrics["sharpe_pnl_unit"])
        self.assertAlmostEqual(metrics["sharpe_daily"], 0.57735, places=5)
        self.assertAlmostEqual(metrics["sharpe_annualized"], 9.165151, places=5)
        self.assertNotIn("pnl_points", feature_columns(df))

    def test_requires_two_distinct_trading_days(self):
        df = pd.DataFrame({
            "timestamp": ["2026-08-03T04:00:00+00:00"],
            "pnl_points": [10.0],
        })
        metrics = _sharpe_metrics(df)
        self.assertEqual(metrics["sharpe_status"], "insufficient_days")
        self.assertIsNone(metrics["sharpe_annualized"])


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
        self.assertIn("trade_pnl", source,
                      "_fetch_ml_collection must request realized P&L for the Sharpe diagnostic")

    def test_fetch_requests_realized_pnl_from_supabase(self):
        """The executed Supabase query includes realized P&L for Sharpe metrics."""
        class Query:
            def __init__(self):
                self.selected_columns = None

            def select(self, columns):
                self.selected_columns = columns
                return self

            def order(self, _column):
                return self

            def range(self, _start, _end):
                return self

            def execute(self):
                return types.SimpleNamespace(data=[])

        query = Query()

        class Supabase:
            def table(self, table_name):
                self.table_name = table_name
                return query

        supabase = Supabase()
        self.assertEqual(_fetch_ml_collection(supabase), [])
        self.assertEqual(supabase.table_name, "ml_collection")
        self.assertIn("trade_pnl", query.selected_columns)


def _training_frame(n=64, feature_names=None):
    """Small deterministic frame for real XGBoost training contract tests."""
    feature_names = feature_names or ["alpha", "beta", "gamma"]
    rows = {
        "timestamp": pd.date_range("2026-07-08", periods=n, freq="min"),
        "label": [i % 2 for i in range(n)],
    }
    for j, name in enumerate(feature_names):
        rows[name] = [float((i * (j + 2)) % 11) for i in range(n)]
    return pd.DataFrame(rows)


def _fake_shap(values, observed=None):
    """External SHAP boundary fake; run_training and XGBoost remain real."""
    class Explanation:
        def __init__(self, vals):
            self.values = np.asarray(vals, dtype=float)

    class TreeExplainer:
        def __init__(self, model):
            self.expected_value = 0.0

        def shap_values(self, X, tree_limit=None, check_additivity=True):
            if observed is not None:
                observed.update({
                    "X": X.copy(),
                    "tree_limit": tree_limit,
                    "check_additivity": check_additivity,
                })
            return np.asarray(values, dtype=float)

        def __call__(self, X, **kwargs):
            return Explanation(np.asarray(values, dtype=float))

    return types.SimpleNamespace(TreeExplainer=TreeExplainer)


class TestOfflineShapContract(unittest.TestCase):
    def _config(self):
        return MLConfig(
            n_estimators=30,
            early_stopping_rounds=5,
            max_depth=2,
        )

    def test_success_schema_types_and_raw_margin_metadata(self):
        df = _training_frame()
        observed = {}
        fake = _fake_shap(
            np.tile([[0.1, -0.2, 0.3]], (len(df) - int(len(df) * 0.8), 1)),
            observed=observed,
        )
        with patch.dict(sys.modules, {"shap": fake}):
            model, metrics = run_training(df, ["alpha", "beta", "gamma"],
                                           config=self._config())
        _, expected_test = chronological_split(df)
        pd.testing.assert_frame_equal(
            observed["X"].reset_index(drop=True),
            expected_test[["alpha", "beta", "gamma"]].reset_index(drop=True),
        )
        self.assertEqual(observed["tree_limit"], int(model.best_iteration) + 1)
        self.assertIs(observed["check_additivity"], True)
        self.assertIs(metrics["shap_computed"], True)
        self.assertIsInstance(metrics["shap_top_features"], list)
        self.assertEqual(metrics["shap_output_unit"], "raw_margin_log_odds")
        self.assertEqual(metrics["shap_explained_rows"], metrics["n_test"])
        self.assertEqual(metrics["shap_backend"], "shap_tree_explainer")
        self.assertEqual(metrics["shap_status"], "computed")
        self.assertIs(metrics["shap_plot_saved"], False)

    def test_top_fifteen_descending_nonnegative_and_feature_aligned(self):
        features = [f"f{i}" for i in range(20)]
        means = np.arange(1, 21, dtype=float)
        values = np.tile(means, (len(_training_frame()) - int(len(_training_frame()) * 0.8), 1))
        fake = _fake_shap(values)
        with patch.dict(sys.modules, {"shap": fake}):
            _, metrics = run_training(_training_frame(feature_names=features), features,
                                       config=self._config())
        top = metrics["shap_top_features"]
        self.assertEqual(len(top), 15)
        self.assertEqual([row["feature"] for row in top], features[-1:-16:-1])
        values = [row["mean_abs_shap"] for row in top]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertTrue(all(value >= 0 for value in values))
        self.assertTrue(all(set(row) == {"feature", "mean_abs_shap"} for row in top))

    def test_held_out_rows_and_early_stopped_native_additivity(self):
        df = _training_frame()
        fake = types.SimpleNamespace(TreeExplainer=lambda model: (_ for _ in ()).throw(
            RuntimeError("incompatible SHAP/XGBoost")))
        with patch.dict(sys.modules, {"shap": fake}):
            model, metrics = run_training(df, ["alpha", "beta", "gamma"],
                                           config=self._config())
        self.assertEqual(metrics["shap_explained_rows"], metrics["n_test"])
        self.assertEqual(metrics["shap_backend"], "xgboost_pred_contribs")
        self.assertTrue(metrics["shap_computed"])
        self.assertIsInstance(metrics["shap_fallback_reason"], str)
        self.assertEqual(metrics["shap_iteration_range"],
                         [0, int(model.best_iteration) + 1])
        self.assertTrue(metrics["shap_raw_margin_additivity"])

    def test_missing_shap_is_graceful_and_persists_model_and_report(self):
        df = _training_frame()
        with tempfile.TemporaryDirectory() as directory:
            model_path = os.path.join(directory, "model.joblib")
            report_path = os.path.join(directory, "metrics.json")
            with patch.dict(sys.modules, {"shap": None}):
                _, metrics = run_training(df, ["alpha", "beta", "gamma"],
                                           config=self._config(), save_path=model_path,
                                           report_path=report_path)
            self.assertFalse(metrics["shap_computed"])
            self.assertEqual(metrics["shap_status"], "shap_unavailable")
            self.assertEqual(metrics["shap_explained_rows"], 0)
            self.assertEqual(metrics["shap_top_features"], [])
            self.assertTrue(os.path.isfile(model_path))
            self.assertTrue(os.path.isfile(report_path))

    def test_plot_file_nonempty_figures_close_and_no_plot_requested(self):
        df = _training_frame()
        fake = _fake_shap(np.tile([[0.1, 0.2, 0.3]], (len(df), 1)))
        import matplotlib.pyplot as plt
        with tempfile.TemporaryDirectory() as directory:
            plot_path = os.path.join(directory, "shap.png")
            with patch.dict(sys.modules, {"shap": fake}):
                _, plotted = run_training(df, ["alpha", "beta", "gamma"],
                                           config=self._config(), shap_plot_path=plot_path)
            self.assertTrue(plotted["shap_plot_saved"])
            self.assertGreater(os.path.getsize(plot_path), 0)
            self.assertEqual(plt.get_fignums(), [])
        with patch.dict(sys.modules, {"shap": fake}):
            _, unplotted = run_training(df, ["alpha", "beta", "gamma"],
                                        config=self._config())
        self.assertFalse(unplotted["shap_plot_saved"])

    def test_plot_failure_is_nonfatal(self):
        df = _training_frame()
        fake = _fake_shap(np.tile([[0.1, 0.2, 0.3]], (len(df), 1)))
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(sys.modules, {"shap": fake}):
                with patch("matplotlib.figure.Figure.savefig",
                           side_effect=OSError("plot unavailable")):
                    _, metrics = run_training(df, ["alpha", "beta", "gamma"],
                                               config=self._config(), shap_plot_path=directory)
        self.assertTrue(metrics["shap_computed"])
        self.assertFalse(metrics["shap_plot_saved"])
        self.assertEqual(metrics["shap_plot_status"], "failed")

    def test_report_contains_shap_payload_and_model_version(self):
        df = _training_frame()
        fake = _fake_shap(np.tile([[0.1, 0.2, 0.3]], (len(df), 1)))
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "metrics.json")
            with patch.dict(sys.modules, {"shap": fake}):
                run_training(df, ["alpha", "beta", "gamma"], config=self._config(),
                             report_path=report_path, model_version="v42")
            with open(report_path) as report_file:
                payload = json.load(report_file)
        self.assertEqual(payload["model_version"], "v42")
        self.assertIn("shap_computed", payload)
        self.assertIn("shap_top_features", payload)

    def test_modern_callable_tree_explainer_and_list_values(self):
        df = _training_frame()
        n_test = len(df) - int(len(df) * 0.8)

        class CallableExplainer:
            def __call__(self, X, **kwargs):
                return types.SimpleNamespace(values=np.ones((len(X), 3)))

        with patch.dict(sys.modules, {"shap": types.SimpleNamespace(
                TreeExplainer=lambda model: CallableExplainer())}):
            _, callable_metrics = run_training(
                df, ["alpha", "beta", "gamma"], config=self._config())
        self.assertTrue(callable_metrics["shap_computed"])
        self.assertEqual(callable_metrics["shap_explained_rows"], n_test)

        class ListExplainer:
            def shap_values(self, X, **kwargs):
                return [np.zeros((len(X), 3)), np.full((len(X), 3), 0.25)]

        with patch.dict(sys.modules, {"shap": types.SimpleNamespace(
                TreeExplainer=lambda model: ListExplainer())}):
            _, list_metrics = run_training(
                df, ["alpha", "beta", "gamma"], config=self._config())
        self.assertTrue(list_metrics["shap_computed"])
        self.assertEqual(list_metrics["shap_top_features"][0]["mean_abs_shap"], 0.25)

    def test_external_nonfinite_values_fall_back_to_native(self):
        df = _training_frame()
        n_test = len(df) - int(len(df) * 0.8)
        fake = _fake_shap(np.full((n_test, 3), np.nan))
        with patch.dict(sys.modules, {"shap": fake}):
            _, metrics = run_training(df, ["alpha", "beta", "gamma"],
                                       config=self._config())
        self.assertTrue(metrics["shap_computed"])
        self.assertEqual(metrics["shap_backend"], "xgboost_pred_contribs")
        self.assertIn("ValueError", metrics["shap_fallback_reason"])
        self.assertEqual(metrics["shap_explained_rows"], metrics["n_test"])

    def test_nested_import_error_falls_back_to_native(self):
        df = _training_frame()
        original_import = builtins.__import__

        def nested_import(name, *args, **kwargs):
            if name == "shap":
                raise ModuleNotFoundError("No module named 'shap._cext'", name="shap._cext")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=nested_import):
            _, metrics = run_training(df, ["alpha", "beta", "gamma"], config=self._config())
        self.assertTrue(metrics["shap_computed"])
        self.assertEqual(metrics["shap_backend"], "xgboost_pred_contribs")
        self.assertEqual(metrics["shap_explained_rows"], metrics["n_test"])
        self.assertIn("ModuleNotFoundError", metrics["shap_fallback_reason"])

    def test_generic_import_failure_falls_back_to_native(self):
        df = _training_frame()
        original_import = builtins.__import__

        def broken_import(name, *args, **kwargs):
            if name == "shap":
                raise RuntimeError("incompatible SHAP runtime")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=broken_import):
            _, metrics = run_training(df, ["alpha", "beta", "gamma"], config=self._config())
        self.assertTrue(metrics["shap_computed"])
        self.assertEqual(metrics["shap_backend"], "xgboost_pred_contribs")
        self.assertEqual(metrics["shap_explained_rows"], metrics["n_test"])
        self.assertIn("RuntimeError", metrics["shap_fallback_reason"])

    def test_native_validation_rejects_malformed_nonfinite_and_nonadditive_outputs(self):
        X_test = pd.DataFrame({"a": [1.0, 2.0], "b": [3.0, 4.0]})

        class Booster:
            def __init__(self, contributions, margins):
                self.contributions = contributions
                self.margins = margins

            def predict(self, dtest, pred_contribs=False, output_margin=False, **kwargs):
                return self.contributions if pred_contribs else self.margins

        class Model:
            def __init__(self, booster):
                self.booster = booster

            def get_booster(self):
                return self.booster

        with self.assertRaisesRegex(ValueError, "shape mismatch"):
            _native_shap_values(Model(Booster(np.ones((2, 2)), np.zeros(2))), X_test, 1)
        with self.assertRaisesRegex(ValueError, "non-finite values"):
            _native_shap_values(Model(Booster(
                np.array([[1.0, 2.0, np.nan], [1.0, 2.0, 3.0]]), np.zeros(2))),
                X_test, 1)
        with self.assertRaisesRegex(ValueError, "raw margins"):
            _native_shap_values(Model(Booster(
                np.ones((2, 3)), np.array([np.nan, 1.0]))), X_test, 1)
        with self.assertRaisesRegex(ValueError, "not additive"):
            _native_shap_values(Model(Booster(
                np.ones((2, 3)), np.zeros(2))), X_test, 1)

    def test_native_fallback_shape_and_total_failure_are_reported(self):
        X_test = pd.DataFrame({"a": [1.0, 2.0], "b": [3.0, 4.0]})

        class Model:
            best_iteration = 1
            n_estimators = 2

        failing_shap = types.SimpleNamespace(
            TreeExplainer=lambda model: (_ for _ in ()).throw(RuntimeError("tree broken")))
        with patch.dict(sys.modules, {"shap": failing_shap}), \
                patch("ml_signal.train_offline._native_shap_values",
                      return_value=(np.ones((2, 1)), (0, 2))):
            metrics = _shap_metrics()
            _compute_shap(Model(), X_test, ["a", "b"], metrics)
        self.assertEqual(metrics["shap_status"], "failed")
        self.assertEqual(metrics["shap_error_type"], "ValueError")

        with patch.dict(sys.modules, {"shap": failing_shap}), \
                patch("ml_signal.train_offline._native_shap_values",
                      side_effect=RuntimeError("native broken")):
            metrics = _shap_metrics()
            _compute_shap(Model(), X_test, ["a", "b"], metrics)
        self.assertEqual(metrics["shap_status"], "failed")
        self.assertEqual(metrics["shap_error_type"], "RuntimeError")

    def test_import_failure_and_native_failure_persist_with_zero_explained_rows(self):
        df = _training_frame()
        original_import = builtins.__import__

        def broken_import(name, *args, **kwargs):
            if name == "shap":
                raise RuntimeError("incompatible SHAP runtime")
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            model_path = os.path.join(directory, "model.joblib")
            report_path = os.path.join(directory, "metrics.json")
            with patch("builtins.__import__", side_effect=broken_import), \
                    patch("ml_signal.train_offline._native_shap_values",
                          side_effect=RuntimeError("native broken")):
                _, metrics = run_training(
                    df, ["alpha", "beta", "gamma"], config=self._config(),
                    save_path=model_path, report_path=report_path)
            self.assertFalse(metrics["shap_computed"])
            self.assertEqual(metrics["shap_status"], "failed")
            self.assertEqual(metrics["shap_explained_rows"], 0)
            self.assertTrue(os.path.isfile(model_path))
            self.assertTrue(os.path.isfile(report_path))

    def test_requested_plot_without_shap_is_nonfatal(self):
        df = _training_frame()
        with tempfile.TemporaryDirectory() as directory:
            plot_path = os.path.join(directory, "missing-shap.png")
            with patch.dict(sys.modules, {"shap": None}):
                _, metrics = run_training(df, ["alpha", "beta", "gamma"],
                                           config=self._config(), shap_plot_path=plot_path)
            self.assertFalse(os.path.exists(plot_path))
        self.assertFalse(metrics["shap_computed"])
        self.assertEqual(metrics["shap_plot_status"], "not_saved_no_shap")

    def test_requested_plot_without_shap_removes_stale_file(self):
        df = _training_frame()
        with tempfile.TemporaryDirectory() as directory:
            plot_path = os.path.join(directory, "stale-shap.png")
            with open(plot_path, "wb") as plot_file:
                plot_file.write(b"stale SHAP plot")
            with patch.dict(sys.modules, {"shap": None}):
                model, metrics = run_training(
                    df, ["alpha", "beta", "gamma"], config=self._config(),
                    shap_plot_path=plot_path,
                )
            self.assertFalse(os.path.exists(plot_path))
        self.assertIsNotNone(model)
        self.assertEqual(metrics["n_samples"], len(df))
        self.assertFalse(metrics["shap_computed"])
        self.assertEqual(metrics["shap_plot_status"], "not_saved_no_shap")

    def test_requested_plot_without_shap_preserves_symlink_and_target(self):
        df = _training_frame()
        with tempfile.TemporaryDirectory() as directory:
            target_path = os.path.join(directory, "shap-target.png")
            plot_path = os.path.join(directory, "shap-link.png")
            with open(target_path, "wb") as target_file:
                target_file.write(b"SHAP plot target")
            os.symlink(target_path, plot_path)
            with patch.dict(sys.modules, {"shap": None}):
                model, metrics = run_training(
                    df, ["alpha", "beta", "gamma"], config=self._config(),
                    shap_plot_path=plot_path,
                )
            self.assertTrue(os.path.islink(plot_path))
            with open(target_path, "rb") as target_file:
                self.assertEqual(target_file.read(), b"SHAP plot target")
        self.assertIsNotNone(model)
        self.assertFalse(metrics["shap_computed"])
        self.assertEqual(metrics["shap_plot_status"], "not_saved_no_shap")

    def test_requested_non_png_plot_without_shap_preserves_regular_file(self):
        df = _training_frame()
        with tempfile.TemporaryDirectory() as directory:
            plot_path = os.path.join(directory, "notes.txt")
            with open(plot_path, "wb") as plot_file:
                plot_file.write(b"unrelated training notes")
            with patch.dict(sys.modules, {"shap": None}):
                model, metrics = run_training(
                    df, ["alpha", "beta", "gamma"], config=self._config(),
                    shap_plot_path=plot_path,
                )
            self.assertTrue(os.path.isfile(plot_path))
            with open(plot_path, "rb") as plot_file:
                self.assertEqual(plot_file.read(), b"unrelated training notes")
        self.assertIsNotNone(model)
        self.assertFalse(metrics["shap_computed"])
        self.assertEqual(metrics["shap_plot_status"], "not_saved_no_shap")

    def test_requested_directory_plot_without_shap_is_nonfatal(self):
        df = _training_frame()
        with tempfile.TemporaryDirectory() as directory:
            plot_path = os.path.join(directory, "existing-plot-directory")
            os.mkdir(plot_path)
            with patch.dict(sys.modules, {"shap": None}):
                model, metrics = run_training(
                    df, ["alpha", "beta", "gamma"], config=self._config(),
                    shap_plot_path=plot_path,
                )
            self.assertTrue(os.path.isdir(plot_path))
        self.assertIsNotNone(model)
        self.assertFalse(metrics["shap_computed"])
        self.assertEqual(metrics["shap_plot_status"], "not_saved_no_shap")

    def test_stale_plot_cleanup_failure_is_nonfatal(self):
        df = _training_frame()
        with tempfile.TemporaryDirectory() as directory:
            plot_path = os.path.join(directory, "stale-shap.png")
            with open(plot_path, "wb") as plot_file:
                plot_file.write(b"stale SHAP plot")
            with patch.dict(sys.modules, {"shap": None}), \
                    patch("ml_signal.train_offline.os.remove",
                          side_effect=OSError("permission denied")):
                model, metrics = run_training(
                    df, ["alpha", "beta", "gamma"], config=self._config(),
                    shap_plot_path=plot_path,
                )
            self.assertTrue(os.path.isfile(plot_path))
        self.assertIsNotNone(model)
        self.assertEqual(metrics["n_samples"], len(df))
        self.assertFalse(metrics["shap_computed"])
        self.assertEqual(metrics["shap_plot_status"], "stale_cleanup_failed")
        self.assertEqual(metrics["shap_plot_error_type"], "OSError")

    def test_report_paths_and_both_shap_summary_output_branches(self):
        paths = _offline_report_paths("/repo", "v42")
        self.assertEqual(paths, (
            "/repo/reports/ml/task183_offline_metrics.json",
            "/repo/reports/ml/v42_offline_metrics.json",
            "/repo/reports/ml/v42_shap_summary.png",
        ))
        computed = _shap_metrics()
        computed.update({"shap_computed": True, "shap_top_features": [
            {"feature": "alpha", "mean_abs_shap": 0.125},
        ]})
        unavailable = _shap_metrics()
        unavailable["shap_status"] = "shap_unavailable"
        for metrics, expected in ((computed, "alpha"), (unavailable, "unavailable")):
            output = tempfile.SpooledTemporaryFile(mode="w+")
            with redirect_stdout(output):
                _print_shap_summary(metrics)
            output.seek(0)
            text = output.read()
            self.assertIn("raw margin/log-odds", text)
            self.assertIn(expected, text)
            output.close()

    def test_main_orchestrates_versioned_shap_report_without_live_io(self):
        from ml_signal.train_offline import main

        frame = _training_frame(n=4, feature_names=["alpha"])
        metrics = {
            "n_samples": 4, "n_train": 3, "n_test": 1,
            "pos_rate": 0.5, "provisional": True, "auc_roc": 0.75,
            "precision": 0.5, "recall": 0.5, "precision_top20": 0.5,
            "top_features": [{"feature": "alpha", "gain": 0.5}],
            "shap_computed": True,
            "shap_top_features": [{"feature": "alpha", "mean_abs_shap": 0.25}],
            "shap_status": "computed",
        }
        captured = {}

        def fake_run_training(*args, **kwargs):
            captured["kwargs"] = kwargs
            return object(), metrics

        fake_supabase = types.SimpleNamespace(create_client=lambda url, key: object())
        fake_dotenv = types.SimpleNamespace(load_dotenv=lambda path: None)
        with patch.dict(os.environ, {"SUPABASE_URL": "url", "SUPABASE_KEY": "key"}), \
                patch.dict(sys.modules, {"supabase": fake_supabase, "dotenv": fake_dotenv}), \
                patch("ml_signal.train_offline._fetch_ml_collection", return_value=[{"row": 1}]), \
                patch("ml_signal.dataset.build_real_outcome_frame", return_value=frame), \
                patch("ml_signal.train_offline.feature_columns", return_value=["alpha"]), \
                patch("ml_signal.predictor.get_next_model_version_and_path",
                      return_value=("/models/v42.joblib", "v42")), \
                patch("ml_signal.train_offline.run_training", side_effect=fake_run_training), \
                patch("builtins.open", mock_open()), \
                patch("ml_signal.train_offline.json.dump"):
            output = tempfile.SpooledTemporaryFile(mode="w+")
            with redirect_stdout(output):
                main()
            output.seek(0)
            stdout = output.read()
            output.close()

        self.assertEqual(captured["kwargs"]["model_version"], "v42")
        self.assertTrue(captured["kwargs"]["shap_plot_path"].endswith(
            "reports/ml/v42_shap_summary.png"))
        self.assertIn("SHAP Feature Importance", stdout)
        self.assertIn("raw margin/log-odds", stdout)
        self.assertIn("alpha", stdout)


if __name__ == "__main__":
    unittest.main()
