import unittest
import numpy as np
import pandas as pd
import json
import tempfile
import os
import sys
import builtins
from unittest.mock import patch
import types

from ml_signal.train_offline import run_training, chronological_split, _native_shap_values, audit_shap_stability
from ml_signal.config import MLConfig
import xgboost as xgb
import pytest
import joblib
from pathlib import Path
from ml_signal import train_offline


@pytest.fixture
def cli_training(monkeypatch, tmp_path):
    """Exercise the CLI refit, persistence and SHAP path with synthetic CV input."""
    trade_times = pd.date_range("2026-08-02", periods=10, freq="h", tz="UTC")
    market_times = pd.date_range("2026-08-01", periods=410, freq="5min", tz="UTC")

    def frame(times):
        return pd.DataFrame({
            "timestamp": times,
            "resolution_timestamp": times + pd.Timedelta(minutes=5),
            "label": np.arange(len(times)) % 2,
            "candle_features__body_pct": np.arange(len(times), dtype=float),
        })

    trade, market = frame(trade_times), frame(market_times)
    metrics = dict(validation_method="walk_forward_purged", evaluable_folds=5,
                   degenerate_folds=0, mean_auc=0.7, min_fold_auc=0.6,
                   ci_95_lower=0.6, brier_score=0.2, leakage_guard_passed=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(train_offline, "__file__", str(tmp_path / "ml_signal/train_offline.py"))
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    monkeypatch.setattr("supabase.create_client", lambda *a: object())
    monkeypatch.setattr(train_offline, "_fetch_ml_collection", lambda client: [])
    monkeypatch.setattr(train_offline, "_fetch_trade_exit_timestamps", lambda client: {})
    monkeypatch.setattr(train_offline.MarketMovementPipeline, "prepare_dataset", lambda *a: market.copy())
    monkeypatch.setattr(train_offline.TradeOutcomePipeline, "prepare_dataset", lambda *a: trade.copy())
    for pipeline in (train_offline.MarketMovementPipeline, train_offline.TradeOutcomePipeline):
        monkeypatch.setattr(pipeline, "run_walk_forward", lambda *a, **kw: (None, metrics.copy()))
    monkeypatch.setitem(sys.modules, "shap", None)
    return trade, market, metrics, tmp_path


@pytest.mark.parametrize("pipeline", ["all", "trade_outcomes"])
@pytest.mark.parametrize("historical_probability", [None, 99.0])
def test_cli_hybrid_refit_uses_fresh_transfer_and_shared_holdout(
    cli_training, monkeypatch, pipeline, historical_probability
):
    trade, market, metrics, root = cli_training
    transfer = "meta_features__market_movement_prob"
    if historical_probability is not None:
        trade[transfer] = historical_probability
    # Cover canonical and unpromoted persistence, including a purged trade row.
    metrics["mean_auc"] = 0.4 if historical_probability is None else 0.7
    promoted = metrics["mean_auc"] >= 0.55
    trade.loc[6, "resolution_timestamp"] = trade.iloc[9]["timestamp"]
    fits, explained = [], []
    original_fit = xgb.XGBClassifier.fit
    original_compute = train_offline._compute_shap

    def fit(model, X, y, *args, **kwargs):
        fits.append(X.copy())
        return original_fit(model, X, y, *args, **kwargs)

    def explain(model, X, *args):
        explained.append(X.copy())
        return original_compute(model, X, *args)

    monkeypatch.setattr(xgb.XGBClassifier, "fit", fit)
    monkeypatch.setattr(train_offline, "_compute_shap", explain)
    train_offline.main(["--pipeline", pipeline])

    model_name = "v1.joblib" if promoted else "v1_unpromoted.joblib"
    bundle = joblib.load(root / "ml_signal/models" / model_name)
    summary = json.loads((root / "reports/ml/task183_offline_metrics.json").read_text())
    assert summary["shap_computed"] is True
    assert summary["promoted"] is promoted
    train_X = fits[-1]
    assert transfer in train_X
    assert train_X[transfer].between(0, 1).all()
    assert train_X[transfer].nunique() > 1
    assert train_X["candle_features__body_pct"].tolist() == [0, 1, 2, 3, 4, 5, 7]
    evaluation = trade.iloc[8:]
    expected_market = market[
        (market["resolution_timestamp"] < evaluation["timestamp"].min())
        & (market["timestamp"] < evaluation["timestamp"].min() - pd.Timedelta(minutes=15))
    ]
    pd.testing.assert_frame_equal(
        fits[-2].reset_index(drop=True),
        expected_market[bundle.stage1_feature_names].reset_index(drop=True),
    )
    for fold_X, test_start in zip(fits[:-2], trade.iloc[5:]["timestamp"]):
        fold_market = market[
            (market["resolution_timestamp"] < test_start)
            & (market["timestamp"] < test_start - pd.Timedelta(minutes=15))
        ]
        pd.testing.assert_frame_equal(
            fold_X.reset_index(drop=True),
            fold_market[bundle.stage1_feature_names].reset_index(drop=True),
        )
    np.testing.assert_allclose(
        explained[0][transfer],
        bundle.stage1_model.predict_proba(evaluation[bundle.stage1_feature_names])[:, 1],
    )
    assert all(Path(p).exists() for p in train_offline._offline_report_paths(str(root), "v1"))


@pytest.mark.parametrize("bounded_subset", ["empty", "single_class"])
def test_cli_rejects_actual_stage1_subset_and_clears_stale_reports(
    cli_training, monkeypatch, bounded_subset
):
    trade, market, _, root = cli_training
    start = "2026-08-02T09:00Z" if bounded_subset == "empty" else "2026-08-02T07:00Z"
    market["timestamp"] = pd.date_range(start, periods=len(market), freq="5min")
    market["resolution_timestamp"] = market["timestamp"] + pd.Timedelta(minutes=5)
    market.loc[market["timestamp"] < trade.iloc[8]["timestamp"], "label"] = 0
    # The old independent 80/20 check passes, but the actual fit subset cannot.
    assert train_offline._final_refit_class_reason(market, "Stage 1") is None
    paths = train_offline._offline_report_paths(str(root), "v1")[1:]
    for path in paths:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"stale")
    monkeypatch.setattr(xgb.XGBClassifier, "fit", lambda *a, **kw: pytest.fail("invalid subset reached fit"))

    with pytest.raises(train_offline.ModelPromotionError):
        train_offline.main(["--enforce-gate"])

    summary = json.loads((root / "reports/ml/task183_offline_metrics.json").read_text())
    audit = json.loads((root / "reports/ml/v1_rejection_audit.json").read_text())
    assert summary["promoted"] is False
    assert any("Stage 1" in reason for reason in summary["gate_reasons"])
    assert audit["reasons"] == summary["gate_reasons"]
    assert not list((root / "ml_signal/models").glob("*.joblib"))
    assert not any(Path(p).exists() for p in paths)


@pytest.mark.parametrize("failure", ["missing_feature", "report_write"])
def test_report_failure_clears_all_versioned_outputs(tmp_path, monkeypatch, failure):
    frame = _training_frame()
    features = ["alpha", "beta", "gamma"]
    model = xgb.XGBClassifier(n_estimators=2).fit(frame[features], frame["label"])
    train, evaluation = chronological_split(frame)
    paths = train_offline._offline_report_paths(str(tmp_path), "v1")
    for path in paths:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"stale")
    other_version = Path(paths[1]).with_name("v2_offline_metrics.json")
    other_version.write_bytes(b"keep")
    monkeypatch.setitem(sys.modules, "shap", None)
    if failure == "missing_feature":
        evaluation = evaluation.drop(columns="alpha")
    else:
        def fail_write(data, stream, **kwargs):
            stream.write("partial JSON")
            raise OSError("disk write failed")
        monkeypatch.setattr(train_offline.json, "dump", fail_write)

    result = train_offline._generate_shap_report(str(tmp_path), "v1", model, train, features, evaluation)

    assert result["shap_status"] == "failed"
    assert not any(Path(p).exists() for p in paths[1:])
    assert Path(paths[0]).read_bytes() == b"stale"
    assert other_version.read_bytes() == b"keep"


@pytest.mark.parametrize("holdout_shift", [False, True])
def test_report_stability_includes_holdout_without_changing_split_metadata(
    tmp_path, monkeypatch, holdout_shift
):
    # Every training window has the same rare positive feature. Only the
    # holdout changes regime, so auditing training alone must miss the shift.
    alpha = np.tile([0.0] * 19 + [1.0], 12)
    train = pd.DataFrame({
        "timestamp": pd.date_range("2026-08-01", periods=240, freq="min", tz="UTC"),
        "alpha": alpha,
        "label": alpha.astype(int),
    })
    evaluation = pd.DataFrame({
        "timestamp": pd.date_range("2026-08-01T04:00Z", periods=60, freq="min"),
        "alpha": np.ones(60) if holdout_shift else alpha[:60],
    })
    model = xgb.XGBClassifier(
        n_estimators=10, max_depth=1, min_child_weight=0, reg_lambda=0,
        random_state=42,
    ).fit(train[["alpha"]], train["label"])
    assert audit_shap_stability(train, ["alpha"], model)["stability_verdict"] == "stable"
    monkeypatch.setitem(sys.modules, "shap", None)

    report = train_offline._generate_shap_report(
        str(tmp_path), "v1", model, train, ["alpha"], evaluation
    )

    expected = "drift_detected" if holdout_shift else "stable"
    assert report["shap_stability_audit"]["stability_verdict"] == expected
    metadata = report["shap_metadata"]
    assert metadata["sample_size_train"] == 240
    assert metadata["sample_size_test"] == 60
    assert metadata["sample_size_total"] == 300
    assert metadata["training_window_end"] == train["timestamp"].max().isoformat()
    assert metadata["testing_window_start"] == evaluation["timestamp"].min().isoformat()
    persisted = json.loads((tmp_path / "reports/ml/v1_offline_metrics.json").read_text())
    assert persisted["shap_stability_audit"] == report["shap_stability_audit"]


@pytest.mark.parametrize("plot_kind", ["summary", "beeswarm"])
@pytest.mark.parametrize("failure", ["import", "render", "partial_save"])
def test_plot_failures_remove_stale_or_partial_png(tmp_path, monkeypatch, plot_kind, failure):
    import matplotlib.pyplot as plt
    from matplotlib.figure import Figure

    path = tmp_path / f"v1_shap_{plot_kind}.png"
    path.write_bytes(b"old image")
    initial_figures = plt.get_fignums()
    if failure == "import":
        original_import = builtins.__import__

        def fail_import(name, *args, **kwargs):
            if name == "matplotlib":
                raise ImportError("unavailable")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fail_import)
    elif failure == "render":
        def fail_render(*args, **kwargs):
            raise RuntimeError("render failed")

        monkeypatch.setattr(plt, "subplots", fail_render)
    else:
        def fail_save(fig, target, **kwargs):
            Path(target).write_bytes(b"partial image")
            raise OSError("save failed")

        monkeypatch.setattr(Figure, "savefig", fail_save)

    if plot_kind == "summary":
        metrics = train_offline._shap_metrics()
        metrics.update(shap_computed=True, shap_top_features=[dict(feature="alpha", mean_abs_shap=1.0)])
        train_offline._save_shap_plot(metrics, str(path))
        assert metrics["shap_plot_status"] == "failed"
        assert metrics["shap_plot_saved"] is False
    else:
        train_offline._save_shap_beeswarm_plot(
            np.ones((2, 1)), pd.DataFrame({"alpha": [1.0, 2.0]}),
            ["alpha"], str(path), "xgboost_pred_contribs",
        )
    assert not path.exists()
    assert plt.get_fignums() == initial_figures


def test_cli_clears_reused_version_even_if_pipeline_raises(cli_training, monkeypatch):
    _, _, _, root = cli_training
    paths = train_offline._offline_report_paths(str(root), "v1")[1:]
    for path in paths:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"stale")

    def fail_pipeline(*args, **kwargs):
        raise RuntimeError("training failed before report generation")

    monkeypatch.setattr(train_offline.MarketMovementPipeline, "run_walk_forward", fail_pipeline)
    with pytest.raises(RuntimeError, match="training failed"):
        train_offline.main([])
    assert not any(Path(p).exists() for p in paths)

def _training_frame(n=64, feature_names=None):
    feature_names = feature_names or ["alpha", "beta", "gamma"]
    rows = {
        "timestamp": pd.date_range("2026-07-08", periods=n, freq="min"),
        "label": [i % 2 for i in range(n)],
    }
    for j, name in enumerate(feature_names):
        rows[name] = [float((i * (j + 2)) % 11) for i in range(n)]
    return pd.DataFrame(rows)

class TestSHAPSupport(unittest.TestCase):
    def setUp(self):
        self.config = MLConfig(n_estimators=10, early_stopping_rounds=2, max_depth=2)

    def test_tier1_mocked_shap_success(self):
        df = _training_frame()
        class Explanation:
            def __init__(self, vals, base_values):
                self.values = vals
                self.base_values = base_values
        class FakeTreeExplainer:
            def __init__(self, model, **kwargs):
                self.model = model

            def _values_and_base(self, X, tree_limit):
                values = np.ones((len(X), 3))
                raw_margin = self.model.get_booster().predict(
                    xgb.DMatrix(X),
                    output_margin=True,
                    iteration_range=(0, tree_limit),
                )
                return values, raw_margin - values.sum(axis=1)

            def __call__(self, X, **kwargs):
                values, base = self._values_and_base(X, kwargs["tree_limit"])
                return Explanation(values, base)

            def shap_values(self, X, **kwargs):
                values, base = self._values_and_base(X, kwargs["tree_limit"])
                self.expected_value = base
                return values
        
        fake_shap = types.SimpleNamespace(TreeExplainer=FakeTreeExplainer)
        with patch.dict(sys.modules, {"shap": fake_shap}):
            model, metrics = run_training(df, ["alpha", "beta", "gamma"], config=self.config)
        self.assertTrue(metrics["shap_computed"])
        self.assertEqual(metrics["shap_backend"], "shap_tree_explainer")
        self.assertTrue(metrics["shap_metadata"]["raw_margin_additivity"])

    def test_tier2_fallback_on_module_not_found(self):
        df = _training_frame()
        with patch.dict(sys.modules, {"shap": None}):
            model, metrics = run_training(df, ["alpha", "beta", "gamma"], config=self.config)
        self.assertTrue(metrics["shap_computed"])
        self.assertEqual(metrics["shap_backend"], "xgboost_pred_contribs")
        self.assertTrue(metrics["shap_metadata"]["raw_margin_additivity"])
        self.assertGreater(len(metrics["shap_top_features"]), 0)

    def test_tier3_fallback_on_runtime_error(self):
        df = _training_frame()
        def broken_explainer(model): raise RuntimeError("tree failure")
        fake_shap = types.SimpleNamespace(TreeExplainer=broken_explainer)
        with patch.dict(sys.modules, {"shap": fake_shap}):
            model, metrics = run_training(df, ["alpha", "beta", "gamma"], config=self.config)
        self.assertTrue(metrics["shap_computed"])
        self.assertEqual(metrics["shap_backend"], "xgboost_pred_contribs")

    def test_tier4_total_failure_graceful(self):
        df = _training_frame()
        def broken_explainer(model): raise RuntimeError("tree failure")
        fake_shap = types.SimpleNamespace(TreeExplainer=broken_explainer)
        with patch.dict(sys.modules, {"shap": fake_shap}):
            with patch("ml_signal.train_offline._native_shap_values", side_effect=ValueError("native failure")):
                model, metrics = run_training(df, ["alpha", "beta", "gamma"], config=self.config)
        self.assertFalse(metrics["shap_computed"])
        self.assertEqual(metrics["shap_status"], "failed")

    def test_tier5_raw_margin_additivity(self):
        df = _training_frame(n=100)
        X = df[["alpha", "beta", "gamma"]]
        y = df["label"]
        model = xgb.XGBClassifier(n_estimators=10)
        model.fit(X, y)
        values, iteration_range = _native_shap_values(model, X, 9)
        self.assertEqual(values.shape, (100, 3))
        # The internal assert inside _native_shap_values already verifies additivity.

    def test_tier6_beeswarm_plots(self):
        df = _training_frame()
        with tempfile.TemporaryDirectory() as d:
            beeswarm_path = os.path.join(d, "beeswarm.png")
            
            # Native fallback plot
            with patch.dict(sys.modules, {"shap": None}):
                run_training(df, ["alpha", "beta", "gamma"], config=self.config, shap_beeswarm_path=beeswarm_path)
            self.assertTrue(os.path.exists(beeswarm_path))
            os.remove(beeswarm_path)

            # SHAP explainer plot
            class FakeTreeExplainer:
                def __init__(self, model, **kwargs):
                    self.model = model

                def __call__(self, X, **kwargs):
                    values = np.ones((len(X), 3))
                    raw_margin = self.model.get_booster().predict(
                        xgb.DMatrix(X),
                        output_margin=True,
                        iteration_range=(0, kwargs["tree_limit"]),
                    )
                    return types.SimpleNamespace(
                        values=values,
                        base_values=raw_margin - values.sum(axis=1),
                    )
            fake_shap = types.SimpleNamespace(
                TreeExplainer=FakeTreeExplainer,
                summary_plot=lambda v, x, show, max_display: None
            )
            with patch.dict(sys.modules, {"shap": fake_shap}):
                run_training(df, ["alpha", "beta", "gamma"], config=self.config, shap_beeswarm_path=beeswarm_path)
            self.assertTrue(os.path.exists(beeswarm_path))

    def test_tier7_drift_audit(self):
        df = _training_frame(n=250)
        X = df[["alpha", "beta", "gamma"]]
        y = df["label"]
        model = xgb.XGBClassifier(n_estimators=10)
        model.fit(X, y)
        
        # Sparse data
        res = audit_shap_stability(df.iloc[:40], ["alpha", "beta", "gamma"], model, min_window_samples=30)
        self.assertEqual(res["status"], "insufficient_data")
        
        # Normal data
        res2 = audit_shap_stability(df, ["alpha", "beta", "gamma"], model, min_window_samples=30)
        self.assertEqual(res2["status"], "computed")
        self.assertIn("mean_rank_correlation", res2)
        self.assertIn("stability_verdict", res2)

    def test_attribution_swing_overrides_stable_rank_and_turnover(self):
        class Booster:
            def __init__(self):
                self.calls = 0

            def predict(self, dtest, pred_contribs=False, **kwargs):
                vectors = (
                    [1.0, 2.0, 3.0],
                    [1.1, 2.1, 6.5],
                    [1.2, 2.2, 13.0],
                    [1.3, 2.3, 26.0],
                )
                values = np.tile(vectors[self.calls], (dtest.num_row(), 1))
                self.calls += 1
                return np.column_stack([values, np.zeros(len(values))])

        booster = Booster()
        model = types.SimpleNamespace(
            best_iteration=0,
            n_estimators=1,
            get_booster=lambda: booster,
        )
        result = audit_shap_stability(
            _training_frame(n=240),
            ["alpha", "beta", "gamma"],
            model,
            min_window_samples=30,
        )

        self.assertEqual(result["status"], "computed")
        self.assertEqual(result["stability_verdict"], "drift_detected")
        self.assertIn("gamma", result["flagged_features"])

    def test_audit_requires_rank_and_turnover_stability_thresholds(self):
        class Booster:
            def __init__(self):
                self.calls = 0

            def predict(self, dtest, pred_contribs=False, **kwargs):
                vectors = (
                    [3.0, 2.0, 1.0],
                    [2.0, 3.0, 1.0],
                    [3.0, 2.0, 1.0],
                    [2.0, 3.0, 1.0],
                )
                values = np.tile(vectors[self.calls], (dtest.num_row(), 1))
                self.calls += 1
                return np.column_stack([values, np.zeros(len(values))])

        booster = Booster()
        model = types.SimpleNamespace(
            best_iteration=0,
            n_estimators=1,
            get_booster=lambda: booster,
        )
        result = audit_shap_stability(
            _training_frame(n=240),
            ["alpha", "beta", "gamma"],
            model,
            min_window_samples=30,
        )

        self.assertEqual(result["stability_verdict"], "drift_detected")
        self.assertFalse(result["flagged_features"])

    def test_tier8_metadata_completeness(self):
        df = _training_frame()
        with patch.dict(sys.modules, {"shap": None}):
            model, metrics = run_training(df, ["alpha", "beta", "gamma"], config=self.config, model_version="v5")
        m = metrics["shap_metadata"]
        self.assertEqual(m["model_version"], "v5")
        self.assertEqual(m["feature_count"], 3)
        self.assertIn("feature_schema_hash", m)
        self.assertIsNotNone(m["training_window_start"])
        self.assertIsNotNone(m["testing_window_end"])
        self.assertEqual(m["sample_size_total"], 64)

if __name__ == '__main__':
    unittest.main()
