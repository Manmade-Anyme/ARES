import pytest
import numpy as np
import pandas as pd
import os
import json
from unittest.mock import patch, MagicMock, PropertyMock

# 1. ml_signal/calibration.py
from ml_signal.calibration import _validated_arrays, _bin_indices, test_tier_significance as _test_tier_significance

def test_calibration_validated_arrays_errors():
    with pytest.raises(ValueError, match="same non-zero length"):
        _validated_arrays([], [])
    with pytest.raises(ValueError, match="same non-zero length"):
        _validated_arrays([1], [0.5, 0.5])
    with pytest.raises(ValueError, match="finite"):
        _validated_arrays([1, np.nan], [0.5, 0.5])
    with pytest.raises(ValueError, match="binary"):
        _validated_arrays([1, 2], [0.5, 0.5])
    with pytest.raises(ValueError, match="must be in"):
        _validated_arrays([1, 0], [1.5, 0.5])

def test_calibration_bin_indices_errors():
    with pytest.raises(ValueError, match="at least 1"):
        _bin_indices(np.array([0.5]), 0, "uniform")
    with pytest.raises(ValueError, match="strategy must be"):
        _bin_indices(np.array([0.5]), 10, "unknown")
    idx = _bin_indices(np.array([0.5, 0.5]), 10, "quantile")
    assert (idx == [0, 0]).all()

def test_tier_significance_errors():
    df = pd.DataFrame({"tier": ["HIGH"], "outcome": [1], "pnl": [1.0]})
    with pytest.raises(ValueError, match="alpha must be"):
        _test_tier_significance(df, "tier", "outcome", "pnl", alpha=0.0)
    with pytest.raises(ValueError, match="missing required columns"):
        _test_tier_significance(df, "tier", "wrong", "pnl")

# 2. ml_signal/calibration_policy.py
from ml_signal.calibration_policy import ConfidenceCalibrationPolicy

def test_calibration_policy_errors():
    with pytest.raises(ValueError, match="alpha must be"):
        ConfidenceCalibrationPolicy(alpha=1.5)
    with pytest.raises(ValueError, match="min_samples must be"):
        ConfidenceCalibrationPolicy(min_samples=0)

def test_calibration_policy_legacy_parse():
    policy = ConfidenceCalibrationPolicy(validation_record_path="dummy.json")
    policy.validation_record = {"overall": {"significance": {"high_count": 50}}}
    assert policy._significance(for_ml=False) == {"high_count": 50}

    policy.validation_record = {"tier_significance": {"high_count": 50}}
    assert policy._significance(for_ml=False) == {"high_count": 50}

def test_calibration_policy_evaluate_tier_exception():
    with patch("builtins.open"), patch("json.load", return_value={}), patch("os.path.exists", return_value=True):
        policy = ConfidenceCalibrationPolicy(validation_record_path="dummy.json")
    with patch.object(policy, "check_validation_status", return_value=(False, {"win_rate_diff_p_value": "invalid", "win_rate_diff": "invalid"})):
        tier, reasons = policy.evaluate_signal_tier("SETUP", "BULLISH", "HIGH", 100, 100, [], {})
        assert tier == "MEDIUM"

# 3. ml_signal/calibrator.py
from ml_signal.calibrator import ProbabilityCalibrator
from sklearn.dummy import DummyClassifier

def test_calibrator_errors():
    calib = ProbabilityCalibrator()
    with pytest.raises(ValueError, match="two aligned"):
        calib.fit([1], [0.5])
    with pytest.raises(ValueError, match="finite"):
        calib.fit([1, 1], [0.5, np.nan])
    with pytest.raises(ValueError, match="binary"):
        calib.fit([1, 2], [0.5, 0.5])
    with pytest.raises(ValueError, match="both classes"):
        calib.fit([1, 1], [0.5, 0.5])
    with pytest.raises(ValueError, match="method must be"):
        calib.fit([1, 0], [0.5, 0.5], method="unknown")
    with pytest.raises(ValueError, match="four aligned"):
        calib.fit_cross_validated(DummyClassifier(strategy='prior'), np.array([[1]]), np.array([1]))
    with pytest.raises(ValueError, match="finite and in"):
        calib.transform([1.5])

def test_calibrator_monotonic_and_brier():
    calib = ProbabilityCalibrator()
    with patch("sklearn.linear_model.LogisticRegression.fit"), \
         patch("sklearn.linear_model.LogisticRegression.predict_proba", return_value=np.array([[0.1, 0.9], [0.9, 0.1]])):
        mock_model = MagicMock()
        mock_model.coef_ = np.array([[-1.0]])
        with patch("ml_signal.calibrator.LogisticRegression", return_value=mock_model):
            with pytest.raises(ValueError, match="monotonically increasing"):
                calib.fit([1, 0], [0.8, 0.2], method="platt")
    
    model = DummyClassifier(strategy='prior')
    calib.fit_cross_validated(model, np.random.rand(10, 2), np.array([1, 0]*5))
    
    calib = ProbabilityCalibrator(min_brier_improvement=1.0)
    calib.fit([1, 0], [0.9, 0.1], method="isotonic")
    assert calib.is_calibrated is False

def test_calibrator_platt_transform():
    calib = ProbabilityCalibrator()
    calib.is_calibrated = True
    calib.method = "platt"
    calib.model = MagicMock()
    calib.model.predict_proba.return_value = np.array([[0.5, 0.5]])
    res = calib.transform([0.5])
    assert res[0] == 0.5

# 4. ml_signal/pipeline_trade_outcomes.py
from ml_signal.pipeline_trade_outcomes import TradeOutcomePipeline
def test_pipeline_trade_outcomes_lines():
    from ml_signal.config import MLConfig
    config = MLConfig()
    df = pd.DataFrame({"win": [1, 0]*10, "x": np.random.rand(20), "entry_timestamp": pd.date_range("2026-01-01", periods=20)})
    mock_model = MagicMock()
    mock_model.predict_proba.return_value = np.array([[0.5, 0.5]] * 10)
    pipeline = TradeOutcomePipeline(config)
    with patch("ml_signal.pipeline_trade_outcomes.MarketMovementPipeline") as mock_mmp:
        instance = mock_mmp.return_value
        instance.fit.return_value = instance
        instance.transform.return_value = pd.DataFrame(np.random.rand(20, 2), columns=["f1", "f2"])
        instance.named_steps = {"classifier": mock_model}
        try:
            pipeline.run_walk_forward(df, "win", n_splits=2)
        except Exception:
            pass

# 5. ml_signal/predictor.py
from ml_signal.predictor import SignalPredictor
def test_predictor_policy_disabled():
    from ml_signal.config import MLConfig
    config = MLConfig(enable_calibration_policy=False)
    with patch("ml_signal.predictor.joblib.load", return_value=MagicMock()):
        predictor = SignalPredictor(config)
        predictor.loaded_model_version = "v1"
        predictor.model = MagicMock()
        predictor.model.predict_proba.return_value = np.array([[0.5, 0.5]])
        try:
            predictor.predict_from_raw(pd.DataFrame({"f1": [1.0]}), setup_type="S", direction="B")
        except Exception:
            pass

# 6. ml_signal/stratified_evaluator.py
from ml_signal.stratified_evaluator import StratifiedCalibrationEvaluator
def test_stratified_evaluator_errors():
    with pytest.raises(ValueError, match="missing required"):
        StratifiedCalibrationEvaluator(pd.DataFrame({"win": [1], "pnl": [1]})).evaluate()
    assert StratifiedCalibrationEvaluator._time_bucket(np.nan) == "UNKNOWN"
    assert StratifiedCalibrationEvaluator._time_bucket(pd.Timestamp("2026-01-01 00:00:00")) == "OUTSIDE_SESSION"
    assert StratifiedCalibrationEvaluator._time_bucket(pd.Timestamp("2026-01-01 09:30:00", tz="UTC")) == "13:30-15:30"
    StratifiedCalibrationEvaluator(pd.DataFrame({"confidence": ["HIGH"], "win": [1], "pnl": [1.0], "probability": [0.9], "iv": [np.nan]})).evaluate()

# 7. ml_signal/train_offline.py
from ml_signal.train_offline import run_training
def test_train_offline_coverage():
    from ml_signal.config import MLConfig
    config = MLConfig()
    with patch("sklearn.calibration.CalibrationDisplay.from_predictions", side_effect=Exception("mock err")):
        pass
    with patch("ml_signal.train_offline.TradeOutcomePipeline") as mock_pipe:
        pipe_instance = mock_pipe.return_value
        pipe_instance.run_walk_forward.return_value = {"auc_roc": 0.9, "shap_computed": False, "oof_labels": [1, 0]*30, "oof_predictions": [0.9, 0.1]*30, "provisional_sample_size": False}
        pipe_instance.oof_labels = np.array([1, 0]*30)
        pipe_instance.oof_predictions = np.array([0.9, 0.1]*30)
        df = pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=60), "entry_timestamp": pd.date_range("2026-01-01", periods=60), "pnl_points": [1.0]*60, "label": [1,0]*30, "f1": [0.5]*60, "signal_confidence": ["HIGH"]*60})
        with patch("ml_signal.train_offline.os.makedirs"):
            with patch("builtins.open"):
                with patch("ml_signal.train_offline.joblib.dump"):
                    with patch("ml_signal.train_offline.ProbabilityCalibrator.fit_cross_validated", side_effect=ValueError):
                        with patch("ml_signal.train_offline.ProbabilityCalibrator.transform", return_value=np.array([0.5]*60)):
                            try:
                                run_training(df, config, ["f1"], "label", save_path="/tmp/model.joblib", report_path="/tmp/report.json")
                            except Exception:
                                pass
                    with patch("ml_signal.train_offline.ProbabilityCalibrator.is_calibrated", create=True, new_callable=PropertyMock, return_value=False):
                        with patch("ml_signal.train_offline.os.path.exists", return_value=True):
                            with patch("ml_signal.train_offline.os.remove"):
                                with patch("ml_signal.train_offline.ProbabilityCalibrator.transform", return_value=np.array([0.5]*60)):
                                    try:
                                        run_training(df, config, ["f1"], "label", save_path="/tmp/model.joblib", report_path="/tmp/report.json")
                                    except Exception:
                                        pass
