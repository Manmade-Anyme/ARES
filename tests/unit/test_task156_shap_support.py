import unittest
import numpy as np
import pandas as pd
import json
import tempfile
import os
import sys
from unittest.mock import patch
import types

from ml_signal.train_offline import run_training, chronological_split, _native_shap_values, audit_shap_stability
from ml_signal.config import MLConfig
import xgboost as xgb

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
            def __init__(self, vals):
                self.values = vals
        class FakeTreeExplainer:
            def __init__(self, model, **kwargs): pass
            def __call__(self, X, **kwargs):
                return Explanation(np.ones((len(X), 3)))
            def shap_values(self, X, **kwargs):
                return np.ones((len(X), 3))
        
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
                def __call__(self, X, **kwargs):
                    class Exp: values = np.ones((len(X), 3))
                    return Exp()
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
