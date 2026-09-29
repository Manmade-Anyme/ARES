"""Public behavior tests for ADR-157 stratified evaluation and runtime policy."""

import json
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
from ml_signal.stratified_evaluator import StratifiedCalibrationEvaluator
from ml_signal.calibration_policy import ConfidenceCalibrationPolicy
from models import Direction, SetupType


def _stratified_frame():
    rows = []
    # Each block occupies a distinct setup, direction, IV regime, and IST time
    # bucket so all four dimensions and JSON serialization are observable.
    cases = [
        (SetupType.FAILED_BREAKOUT, Direction.BULLISH, 10.0, "2026-09-01T09:30:00+05:30"),
        (SetupType.OI_WALL_REJECTION, Direction.BEARISH, 20.0, "2026-09-01T11:00:00+05:30"),
        (SetupType.EXHAUSTION_REVERSAL, Direction.BULLISH, 30.0, "2026-09-01T14:00:00+05:30"),
    ]
    for setup, direction, iv_level, timestamp in cases:
        for i in range(12):
            rows.append({
                "setup_type": setup.value,
                "direction": direction.value,
                "iv_level": iv_level,
                "timestamp": timestamp,
                "confidence": "HIGH" if i < 6 else "MEDIUM",
                "outcome": int(i % 2 == 0),
                "pnl": 2.0 if i % 2 == 0 else -1.0,
                "probability": 0.6 if i % 2 == 0 else 0.4,
            })
    return pd.DataFrame(rows)


def test_evaluator_stratifies_across_four_dimensions_and_exports_json(tmp_path):
    evaluator = StratifiedCalibrationEvaluator(_stratified_frame())

    report = evaluator.evaluate(alpha=0.05)
    target = tmp_path / "stratified-report.json"
    evaluator.export_json(target)

    assert report.overall["sample_size"] == 36
    for dimension in (
        "by_setup",
        "by_direction",
        "by_volatility_regime",
        "by_time_of_day",
    ):
        strata = getattr(report, dimension)
        assert strata
        assert all("sample_size" in row for row in strata.values())
        assert all("by_tier" in row for row in strata.values())
        assert all("significance" in row for row in strata.values())
        assert all("brier" in row for row in strata.values())
        assert all("calibration" in row for row in strata.values())

    # There must be one stratum for each observed value in the first two axes.
    assert len(report.by_setup) == 3
    assert len(report.by_direction) == 2
    assert target.exists()
    with target.open() as handle:
        serialized = json.load(handle)
    assert all(key in serialized for key in ("by_setup", "by_direction", "by_volatility_regime", "by_time_of_day"))
    assert set(report.by_volatility_regime) == {"LOW_IV", "HIGH_IV"}
    assert set(report.by_time_of_day) == {"09:15-10:30", "10:30-13:30", "13:30-15:30"}


def test_missing_iv_is_reported_as_unknown():
    frame = _stratified_frame()
    frame.loc[frame.index[0], "iv_level"] = None

    report = StratifiedCalibrationEvaluator(frame).evaluate()

    assert "UNKNOWN" in report.by_volatility_regime
    assert report.by_volatility_regime["UNKNOWN"]["sample_size"] == 1


def test_policy_downgrades_unvalidated_high_tier():
    context = {"iv_level": 18.0, "timestamp": datetime(2026, 9, 1, 10, 0)}
    reasons = ["setup heuristic score supports HIGH"]
    policy = ConfidenceCalibrationPolicy()

    tier, updated_reasons = policy.evaluate_signal_tier(
        setup_type=SetupType.FAILED_BREAKOUT,
        direction=Direction.BULLISH,
        tentative_tier="HIGH",
        heuristic_score=8,
        max_score=10,
        reasons=reasons,
        market_context=context,
    )

    assert tier == "MEDIUM"
    assert any(reason.startswith("[CONFIDENCE GATE] HIGH tier suppressed to MEDIUM:") for reason in updated_reasons)
    assert context["uncalibrated_high_suppressed"] is True


def test_policy_preserves_tentative_high_for_shadow_validation(tmp_path):
    signal = SimpleNamespace(
        setup_type=SetupType.FAILED_BREAKOUT,
        direction=Direction.BULLISH,
        confidence="HIGH",
        reasons=[],
        market_context={},
    )
    policy = ConfidenceCalibrationPolicy(str(tmp_path / "missing-record.json"))

    policy.apply_to_signal(signal)
    policy.apply_to_signal(signal)

    assert signal.confidence == "MEDIUM"
    assert signal.market_context["tentative_confidence"] == "HIGH"
    assert signal.market_context["uncalibrated_high_suppressed"] is True


def test_policy_preserves_non_high_tier():
    policy = ConfidenceCalibrationPolicy()
    context = {}

    tier, reasons = policy.evaluate_signal_tier(
        setup_type=SetupType.OI_WALL_REJECTION,
        direction=Direction.BEARISH,
        tentative_tier="MEDIUM",
        heuristic_score=4,
        max_score=8,
        reasons=["medium setup"],
        market_context=context,
    )

    assert tier == "MEDIUM"
    assert reasons == ["medium setup"]


def test_inverted_auc_disables_ml_high_but_does_not_change_detector_gate(tmp_path):
    record = {
        "high_count": 40,
        "medium_count": 60,
        "high_win_rate": 0.7,
        "medium_win_rate": 0.5,
        "high_expectancy": 4.0,
        "medium_expectancy": 1.0,
        "win_rate_diff": 0.2,
        "expectancy_diff": 3.0,
        "fisher_p_value": 0.01,
        "mann_whitney_p_value": 0.01,
        "is_statistically_superior": True,
        "auc_roc": 0.49,
        "probability_calibrated": True,
        "model_version": "v1",
    }
    record_path = tmp_path / "validation.json"
    record_path.write_text(json.dumps(record))
    significant = {
        "high_count": 40,
        "medium_count": 60,
        "win_rate_diff": 0.2,
        "expectancy_diff": 3.0,
        "fisher_p_value": 0.01,
        "mann_whitney_p_value": 0.01,
        "is_statistically_superior": True,
    }
    stratified_path = tmp_path / "confidence_calibration_stratified_report.json"
    stratified_path.write_text(json.dumps({
        "by_setup": {"FAILED_BREAKOUT": {"significance": significant}},
        "by_direction": {"BULLISH": {"significance": significant}},
    }))
    policy = ConfidenceCalibrationPolicy(str(record_path))

    detector_tier, _ = policy.evaluate_signal_tier(
        "FAILED_BREAKOUT", "BULLISH", "HIGH", 4, 4, [], {}
    )
    ml_tier, _ = policy.evaluate_signal_tier(
        "ML_PREDICTION", "UNKNOWN", "HIGH", 0, 0, [], {"model_version": "v1"}
    )

    assert detector_tier == "HIGH"
    assert ml_tier == "MEDIUM"
