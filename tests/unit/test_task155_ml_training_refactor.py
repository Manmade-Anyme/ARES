import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from ml_signal.leakage_guards import (
    assert_no_outcome_leakage,
    assert_chronological_integrity,
    assert_train_test_purged,
    deduplicate_snapshots,
    DataLeakageError
)
from ml_signal.validation import WalkForwardPurgedCV
from ml_signal.promotion_gate import evaluate_promotion_gate, enforce_promotion_or_raise, ModelPromotionError
from ml_signal.dataset import _META_COLS, build_labeled_frame
from ml_signal.config import MLConfig
from ml_signal.train_offline import _final_refit_sample_size_reason


@pytest.mark.parametrize("n_samples", [0, 1, 5])
def test_final_refit_rejects_datasets_not_larger_than_fold_count(n_samples):
    reason = _final_refit_sample_size_reason(n_samples, n_splits=5)

    assert reason is not None
    assert f"got {n_samples}" in reason


def test_final_refit_accepts_dataset_larger_than_fold_count():
    assert _final_refit_sample_size_reason(6, n_splits=5) is None

def test_no_outcome_leakage():
    # Should pass
    assert_no_outcome_leakage(["candle_features__body_pct", "iv_features__iv_level"])
    
    # Should fail
    with pytest.raises(DataLeakageError):
        assert_no_outcome_leakage(["candle_features__body_pct", "trade_id"])

def test_chronological_integrity():
    df_valid = pd.DataFrame({
        "timestamp": [pd.Timestamp("2026-01-01 10:00"), pd.Timestamp("2026-01-01 10:01")]
    })
    assert_chronological_integrity(df_valid)
    
    df_invalid = pd.DataFrame({
        "timestamp": [pd.Timestamp("2026-01-01 10:01"), pd.Timestamp("2026-01-01 10:00")]
    })
    with pytest.raises(DataLeakageError):
        assert_chronological_integrity(df_invalid)

def test_train_test_purged():
    train_df = pd.DataFrame({
        "resolution_timestamp": [pd.Timestamp("2026-01-01 10:05"), pd.Timestamp("2026-01-01 10:10")]
    })
    test_df = pd.DataFrame({
        "timestamp": [pd.Timestamp("2026-01-01 10:11"), pd.Timestamp("2026-01-01 10:15")]
    })
    assert_train_test_purged(train_df, test_df)

    train_df_leak = pd.DataFrame({
        "resolution_timestamp": [pd.Timestamp("2026-01-01 10:05"), pd.Timestamp("2026-01-01 10:12")]
    })
    with pytest.raises(DataLeakageError):
        assert_train_test_purged(train_df_leak, test_df)

def test_walk_forward_purged_cv_embargo():
    dates = pd.date_range("2026-01-01 10:00", periods=40, freq="min")
    df = pd.DataFrame({
        "timestamp": dates,
        "resolution_timestamp": dates + pd.Timedelta(minutes=5),
        "feature": np.random.randn(40)
    })
    cv = WalkForwardPurgedCV(n_splits=2, min_train_samples=2, embargo_window=pd.Timedelta(minutes=10))
    splits = list(cv.split(df))
    assert len(splits) == 2
    
    for train_idx, test_idx, fold_info in splits:
        train_ts = df.iloc[train_idx]["timestamp"]
        test_ts = df.iloc[test_idx]["timestamp"]
        test_start = test_ts.min()
        
        # Check purge: no train row's resolution >= test_start
        train_res = df.iloc[train_idx]["resolution_timestamp"]
        assert (train_res < test_start).all()
        
        # Check embargo: no train row's timestamp >= test_start - embargo_window
        embargo_start = test_start - pd.Timedelta(minutes=10)
        assert (train_ts < embargo_start).all()

def test_evaluate_promotion_gate_success():
    metrics = {
        "validation_method": "walk_forward_purged",
        "evaluable_folds": 4,
        "degenerate_folds": 0,
        "mean_auc": 0.56,
        "ci_95_lower": 0.51,
        "min_fold_auc": 0.42,
        "brier_score": 0.20,
        "leakage_guard_passed": True
    }
    passed, reasons = evaluate_promotion_gate(metrics)
    assert passed is True
    enforce_promotion_or_raise(metrics)

def test_evaluate_promotion_gate_failure():
    metrics = {
        "validation_method": "walk_forward_purged",
        "evaluable_folds": 3,  # Fails min 4
        "degenerate_folds": 0,
        "mean_auc": 0.56,
        "ci_95_lower": 0.51,
        "min_fold_auc": 0.42,
        "brier_score": 0.20,
        "leakage_guard_passed": True
    }
    passed, reasons = evaluate_promotion_gate(metrics)
    assert passed is False
    with pytest.raises(ModelPromotionError):
        enforce_promotion_or_raise(metrics)

def test_deduplicate_snapshots():
    # Multiple polls of one source candle must collapse even when features
    # update during the minute; the terminal state is the canonical snapshot.
    df = pd.DataFrame({
        "timestamp": [
            pd.Timestamp("2026-01-01 10:00:10"),
            pd.Timestamp("2026-01-01 10:00:20"),
            pd.Timestamp("2026-01-01 10:00:30"),
            pd.Timestamp("2026-01-01 10:01:05"),
        ],
        "feature1": [1.0, 1.0, 2.0, 3.0],
        "feature2": [5.0, 5.0, 6.0, 7.0],
        "trade_id": [None, None, None, None],
    })
    dedup = deduplicate_snapshots(df)
    assert len(dedup) == 2
    assert dedup.iloc[0]["timestamp"] == pd.Timestamp("2026-01-01 10:00:30")
    assert dedup.iloc[0]["feature1"] == 2.0
    assert dedup.iloc[1]["timestamp"] == pd.Timestamp("2026-01-01 10:01:05")


def test_deduplicate_snapshots_preserves_distinct_trades_in_same_minute():
    df = pd.DataFrame({
        "timestamp": [
            pd.Timestamp("2026-01-01 10:00:10"),
            pd.Timestamp("2026-01-01 10:00:20"),
        ],
        "feature1": [1.0, 1.0],
        "trade_id": ["trade-a", "trade-b"],
    })

    dedup = deduplicate_snapshots(df)

    assert dedup["trade_id"].tolist() == ["trade-a", "trade-b"]


def test_build_labeled_frame_uses_one_terminal_snapshot_per_candle():
    rows = [
        {"timestamp": "2026-01-01T10:00:00", "raw_candle": {"close": 100}},
        {
            "timestamp": "2026-01-01T10:01:05",
            "raw_candle": {"close": 100},
            "candle_features": {"poll_state": 1},
        },
        {
            "timestamp": "2026-01-01T10:01:55",
            "raw_candle": {"close": 100},
            "candle_features": {"poll_state": 2},
        },
        {"timestamp": "2026-01-01T10:02:00", "raw_candle": {"close": 100}},
        {"timestamp": "2026-01-01T10:03:00", "raw_candle": {"close": 120}},
    ]

    labeled = build_labeled_frame(rows, lookforward=3, tp_points=15, sl_points=10)

    first_candle = labeled.loc[labeled["timestamp"] == pd.Timestamp("2026-01-01 10:00:00")]
    assert first_candle.iloc[0]["label"] == 1
    assert first_candle.iloc[0]["resolution_timestamp"] == pd.Timestamp("2026-01-01 10:03:00")
    assert labeled["timestamp"].dt.floor("min").is_unique


def test_dataset_meta_cols():
    assert "trade_id" in _META_COLS
    assert "snapshot_uuid" in _META_COLS
    assert "signal_id" in _META_COLS
    assert "signal_setup_type" in _META_COLS
    assert "resolution_timestamp" in _META_COLS
