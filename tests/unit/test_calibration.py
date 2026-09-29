"""Public behavior tests for ADR-157 calibration metrics."""

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ml_signal.calibration import (
    compute_brier_decomposition,
    compute_calibration_curve,
    test_tier_significance as compute_tier_significance,
)
from ml_signal.calibrator import ProbabilityCalibrator
from ml_signal.dataset import feature_columns, flatten_features
from ml_signal.config import MLConfig
from ml_signal.train_offline import _write_walk_forward_calibration_artifacts
from ml_signal.predictor import SignalPredictor


def test_brier_decomposition_preserves_murphy_identity():
    y_true = np.array([0, 0, 1, 1])
    y_prob = np.array([0.1, 0.1, 0.9, 0.9])

    result = compute_brier_decomposition(y_true, y_prob, n_bins=2)

    assert result.brier_score == pytest.approx(0.01)
    assert result.reliability == pytest.approx(0.01)
    assert result.resolution == pytest.approx(0.25)
    assert result.uncertainty == pytest.approx(0.25)
    assert result.base_rate == pytest.approx(0.5)
    assert result.brier_score == pytest.approx(
        result.reliability - result.resolution + result.uncertainty
    )


def test_brier_decomposition_includes_within_bin_covariance():
    result = compute_brier_decomposition([0, 1], [0.1, 0.9], n_bins=1)

    assert result.within_bin_variance == pytest.approx(0.16)
    assert result.within_bin_covariance == pytest.approx(0.2)
    assert result.brier_score == pytest.approx(0.01)
    assert result.brier_score == pytest.approx(
        result.reliability - result.resolution + result.uncertainty
    )


def test_calibration_curve_ece_mce_and_coordinates():
    y_true = np.array([0, 0, 1, 1])
    y_prob = np.array([0.1, 0.2, 0.8, 0.9])

    result = compute_calibration_curve(y_true, y_prob, n_bins=2)

    # Coordinates use mean forecast confidence among observations in each bin.
    assert result.bin_centers == pytest.approx([0.15, 0.85])
    assert result.bin_accuracies == pytest.approx([0.0, 1.0])
    assert result.bin_confidences == pytest.approx([0.15, 0.85])
    assert result.bin_counts == [2, 2]
    assert result.ece == pytest.approx(0.15)
    assert result.mce == pytest.approx(0.15)


def _audit_frame():
    """Reproduce the requested 233-trade audit aggregates deterministically."""
    high_count, medium_count = 38, 195
    rows = []

    # Preserve the audited win totals while spreading PnL around each group's
    # audited expectancy; this avoids inventing a rank-perfect PnL distribution.
    for tier, count, wins, pnl_total in (
        ("HIGH", high_count, 12, 50.0),
        ("MEDIUM", medium_count, 63, 220.85),
    ):
        mean_pnl = pnl_total / count
        offsets = np.resize(np.array([-4.0, -2.0, 0.0, 2.0, 4.0]), count)
        offsets -= offsets.mean()
        pnls = mean_pnl + offsets
        pnls[-1] += pnl_total - pnls.sum()
        for i in range(count):
            rows.append({
                "tier": tier,
                "won": int(i < wins),
                "pnl": float(pnls[i]),
            })
    return pd.DataFrame(rows)


def test_tier_significance_reports_233_trade_audit_without_superiority():
    result = compute_tier_significance(
        _audit_frame(),
        tier_col="tier",
        outcome_col="won",
        pnl_col="pnl",
        alpha=0.05,
        min_samples=30,
    )

    assert result.high_count == 38
    assert result.medium_count == 195
    assert result.high_win_rate == pytest.approx(12 / 38)
    assert result.medium_win_rate == pytest.approx(63 / 195)
    assert result.high_expectancy == pytest.approx(50 / 38)
    assert result.medium_expectancy == pytest.approx(220.85 / 195)
    assert result.is_statistically_superior is False
    assert result.verdict == "INVERTED"
    assert 0 <= result.fisher_p_value <= 1
    assert 0 <= result.mann_whitney_p_value <= 1


def test_tier_significance_requires_minimum_samples():
    df = pd.DataFrame({
        "tier": ["HIGH"] * 5 + ["MEDIUM"] * 5,
        "won": [1, 1, 1, 1, 1, 0, 0, 0, 0, 0],
        "pnl": [5.0] * 5 + [-1.0] * 5,
    })

    result = compute_tier_significance(
        df, "tier", "won", "pnl", min_samples=30
    )

    assert result.is_statistically_superior is False
    assert result.verdict == "INSIGNIFICANT"


def test_isotonic_calibrator_is_monotonic_and_only_keeps_brier_improvement():
    y_true = np.array([0, 0, 0, 1, 1, 1])
    y_prob = np.array([0.1, 0.2, 0.4, 0.6, 0.8, 0.9])
    calibrator = ProbabilityCalibrator("isotonic").fit(y_true, y_prob)

    assert calibrator.is_calibrated
    calibrated = calibrator.transform(np.linspace(0, 1, 101))
    assert np.all(np.diff(calibrated) >= -1e-10)
    assert calibrator.brier_after < calibrator.brier_before


def test_platt_calibrator_rejects_inverted_probability_mapping():
    y_true = np.array([0, 0, 0, 1, 1, 1])
    y_prob = np.array([0.9, 0.8, 0.7, 0.3, 0.2, 0.1])
    calibrator = ProbabilityCalibrator("platt")

    with pytest.raises(ValueError, match="not monotonically increasing"):
        calibrator.fit(y_true, y_prob)

    assert calibrator.is_calibrated is False
    assert calibrator.rejection_reason == "non_monotonic_fit"


def test_flattened_outcomes_preserve_signal_tiers_outside_model_features():
    row = {
        "timestamp": "2026-09-01T09:30:00+05:30",
        "raw_candle": {"close": 100.0},
        "signal_setup_type": "FAILED_BREAKOUT",
        "signal_direction": "BULLISH",
        "signal_confidence": "MEDIUM",
        "meta_features": {"signal_tentative_confidence": "HIGH"},
    }
    frame = flatten_features([row])

    assert frame.loc[0, "signal_setup_type"] == "FAILED_BREAKOUT"
    assert frame.loc[0, "signal_direction"] == "BULLISH"
    assert frame.loc[0, "signal_confidence"] == "MEDIUM"
    assert frame.loc[0, "signal_tentative_confidence"] == "HIGH"
    assert not {
        "signal_setup_type", "signal_direction", "signal_confidence",
        "signal_tentative_confidence",
    } & set(feature_columns(frame))


def test_walk_forward_artifacts_write_runtime_record_and_reports(tmp_path):
    n = 80
    labels = np.tile(np.array([0, 1]), n // 2)
    probability = np.linspace(0.05, 0.95, n)
    frame = pd.DataFrame({
        "label": labels,
        "pnl_points": np.where(labels == 1, 2.0, -1.0),
        "signal_confidence": "MEDIUM",
        "signal_tentative_confidence": np.where(np.arange(n) % 2, "HIGH", "MEDIUM"),
        "signal_setup_type": np.where(np.arange(n) % 2, "FAILED_BREAKOUT", "OI_WALL_REJECTION"),
        "signal_direction": np.where(np.arange(n) % 2, "BULLISH", "BEARISH"),
        "iv_features__iv_level": np.linspace(8, 30, n),
        "timestamp": pd.date_range("2026-09-01 09:15", periods=n, freq="min", tz="Asia/Kolkata"),
    })

    calibrator, metrics = _write_walk_forward_calibration_artifacts(
        frame, probability, np.ones(n, dtype=bool), str(tmp_path), "v_test", 0.6, 0.05,
        MLConfig(),
    )

    record_path = tmp_path / "calibration_validation_record.json"
    stratified_path = tmp_path / "confidence_calibration_stratified_report.json"
    assert record_path.exists()
    assert stratified_path.exists()
    record = json.loads(record_path.read_text())
    assert record["model_version"] == "v_test"
    assert "ml_tier_significance" in record
    assert "tier_significance" in record
    assert record["tier_significance"]["high_count"] == 12
    assert record["tier_significance"]["medium_count"] == 12
    assert metrics["calibration_fit_samples"] == 56
    assert metrics["calibration_samples"] == 24
    assert record["probability_calibrated"] is (calibrator is not None)


def test_walk_forward_rejects_fit_that_worsens_heldout_brier(tmp_path):
    n = 80
    labels = np.tile([0, 1], n // 2)
    probability = np.where(labels == 1, 0.8, 0.2).astype(float)
    labels[56:] = 1 - labels[56:]
    frame = pd.DataFrame({
        "label": labels,
        "pnl_points": np.where(labels == 1, 1.0, -1.0),
        "signal_tentative_confidence": np.where(probability >= 0.7, "HIGH", "MEDIUM"),
        "signal_confidence": "MEDIUM",
        "timestamp": pd.date_range("2026-09-01 09:15", periods=n, freq="min", tz="Asia/Kolkata"),
    })

    calibrator, metrics = _write_walk_forward_calibration_artifacts(
        frame, probability, np.ones(n, dtype=bool), str(tmp_path), "v_rejected",
        0.6, 0.05, MLConfig(),
    )

    record = json.loads((tmp_path / "calibration_validation_record.json").read_text())
    assert calibrator is None
    assert record["probability_calibrated"] is False
    assert metrics["calibration_status"] == "no_heldout_brier_improvement"
    assert metrics["calibration_brier_decomposition"]["brier_score"] == pytest.approx(0.64)


@pytest.mark.parametrize("sidecar_exists,record_approved,expected_probability,expected_tier", [
    (False, True, 0.8, "MEDIUM"),
    (True, False, 0.8, "MEDIUM"),
    (True, True, 0.6, "MEDIUM"),
])
def test_live_calibrator_requires_sidecar_and_heldout_approval(
    tmp_path, monkeypatch, sidecar_exists, record_approved, expected_probability, expected_tier,
):
    validation_path = tmp_path / "validation.json"
    validation_path.write_text(json.dumps({
        "model_version": "v1",
        "probability_calibrated": record_approved,
        "ml_tier_significance": {
            "high_count": 40, "medium_count": 40,
            "is_statistically_superior": True,
            "fisher_p_value": 0.001, "mann_whitney_p_value": 0.001,
            "win_rate_diff": 0.2, "expectancy_diff": 1.0,
        },
    }))
    predictor = SignalPredictor(MLConfig(calibration_record_path=str(validation_path)))
    model_path = tmp_path / "v1.joblib"
    model_path.touch()
    model = SimpleNamespace(predict_proba=lambda _: np.array([[0.2, 0.8]]))
    sidecar = SimpleNamespace(is_calibrated=True, transform=lambda _: np.array([0.6]))
    monkeypatch.setattr("ml_signal.predictor.os.path.isfile", lambda _: sidecar_exists)
    monkeypatch.setattr(
        "ml_signal.predictor.joblib.load",
        lambda path: sidecar if str(path).endswith(".calibrator.joblib") else model,
    )
    monkeypatch.setattr("ml_signal.predictor.build_feature_vector", lambda **_: {"f": 1.0})
    predictor.load_model(str(model_path))

    result = predictor.predict_from_raw(
        candle={}, volume_history=[], iv_history=None, atm_ce=None, atm_pe=None,
        total_ce_oi=None, total_pe_oi=None, all_ce_oi=None, all_pe_oi=None,
        levels=[], timestamp="2026-09-01T09:30:00+05:30", spot=100.0,
    )

    assert result["probability"] == expected_probability
    assert result["confidence_tier"] == expected_tier
    assert result["probability_calibrated"] is (sidecar_exists and record_approved)
    assert result["market_context"]["tentative_confidence"] == (
        "HIGH" if expected_probability >= predictor.config.high_threshold else "MEDIUM"
    )
    assert result["market_context"].get("uncalibrated_high_suppressed", False) is (
        expected_probability >= predictor.config.high_threshold
    )
    if not sidecar_exists:
        assert "runtime calibrator unavailable" in result["reasons"][0]
    elif not record_approved:
        assert "no held-out approval" in result["reasons"][0]
