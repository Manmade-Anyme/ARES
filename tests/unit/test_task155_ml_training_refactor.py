import json

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
from ml_signal.validation import WalkForwardPurgedCV, build_fold_result, compute_cv_metrics
from ml_signal.promotion_gate import evaluate_promotion_gate, enforce_promotion_or_raise, ModelPromotionError
from ml_signal.dataset import _META_COLS, build_labeled_frame
from ml_signal.config import MLConfig
from ml_signal import train_offline
from ml_signal.train_offline import _final_refit_sample_size_reason


@pytest.mark.parametrize("folds", [0, 1, -1, 2, 3])
def test_main_rejects_fold_counts_below_promotion_minimum_before_io(
    monkeypatch, folds, capsys
):
    monkeypatch.setattr(
        train_offline,
        "_fetch_ml_collection",
        lambda client: pytest.fail("invalid CLI input must fail before data access"),
    )

    with pytest.raises(SystemExit) as exc_info:
        train_offline.main(["--folds", str(folds)])

    assert exc_info.value.code == 2
    assert "--folds must be at least 4" in capsys.readouterr().err


def test_main_accepts_promotion_minimum_fold_count_before_environment_check(
    monkeypatch, capsys
):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr("config.settings.supabase_url", "")
    monkeypatch.setattr("config.settings.supabase_key", "")

    train_offline.main(["--folds", "4"])

    assert "SUPABASE_URL / SUPABASE_KEY not available" in capsys.readouterr().out


@pytest.mark.parametrize("n_samples", [0, 1, 5])
def test_final_refit_rejects_datasets_not_larger_than_fold_count(n_samples):
    reason = _final_refit_sample_size_reason(n_samples, n_splits=5)

    assert reason is not None
    assert f"got {n_samples}" in reason


def test_final_refit_accepts_dataset_larger_than_fold_count():
    assert _final_refit_sample_size_reason(6, n_splits=5) is None


def test_final_hybrid_refit_rejects_empty_stage1_frame_cleanly(
    monkeypatch, tmp_path
):
    trade_df = pd.DataFrame({
        "timestamp": pd.date_range("2026-08-01", periods=6, freq="h", tz="UTC"),
        "label": [0, 1, 0, 1, 0, 1],
    })
    empty_market_df = pd.DataFrame()
    rejected_metrics = {
        "validation_method": "walk_forward_purged",
        "evaluable_folds": 0,
        "degenerate_folds": 5,
        "mean_auc": None,
        "ci_95_lower": None,
        "min_fold_auc": None,
        "brier_score": None,
        "leakage_guard_passed": True,
    }

    class FakeMarketMovementPipeline:
        def __init__(self, config):
            pass

        def prepare_dataset(self, rows):
            return empty_market_df.copy()

    class FakeTradeOutcomePipeline:
        def __init__(self, config, use_hybrid_transfer):
            assert use_hybrid_transfer is True

        def prepare_dataset(self, rows):
            return trade_df.copy()

        def run_walk_forward(self, frame, market_snapshots_df, n_splits):
            assert market_snapshots_df.empty
            return None, rejected_metrics.copy()

    metrics_path = tmp_path / "metrics.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    monkeypatch.setattr("supabase.create_client", lambda url, key: object())
    monkeypatch.setattr(train_offline, "_fetch_ml_collection", lambda client: [])
    monkeypatch.setattr(train_offline, "_fetch_trade_exit_timestamps", lambda client: {})
    monkeypatch.setattr(train_offline, "MarketMovementPipeline", FakeMarketMovementPipeline)
    monkeypatch.setattr(train_offline, "TradeOutcomePipeline", FakeTradeOutcomePipeline)
    monkeypatch.setattr(
        "ml_signal.predictor.get_next_model_version_and_path",
        lambda models_dir: (str(tmp_path / "v999.joblib"), "v999"),
    )
    monkeypatch.setattr(
        train_offline.xgb,
        "XGBClassifier",
        lambda *args, **kwargs: pytest.fail(
            "an empty Stage 1 frame must short-circuit final hybrid fitting"
        ),
    )

    train_offline.main([
        "--pipeline", "trade_outcomes",
        "--hybrid",
        "--promote",
        "--metrics-path", str(metrics_path),
    ])

    summary = json.loads(metrics_path.read_text())
    audit = json.loads(
        (tmp_path / "reports/ml/v999_rejection_audit.json").read_text()
    )
    assert summary["promoted"] is False
    assert any("stage 1" in reason.lower() for reason in summary["gate_reasons"])
    assert audit["reasons"] == summary["gate_reasons"]
    assert not (tmp_path / "v999.joblib").exists()
    assert not (tmp_path / "v999_unpromoted.joblib").exists()


def test_primary_summary_computes_sharpe_from_realized_trade_dataset(
    monkeypatch, tmp_path
):
    trade_df = pd.DataFrame({
        "timestamp": pd.to_datetime([
            "2026-08-03T04:00:00Z",
            "2026-08-03T05:00:00Z",
            "2026-08-04T04:00:00Z",
            "2026-08-05T04:00:00Z",
        ]),
        "entry_timestamp": pd.to_datetime([
            "2026-08-03T04:00:00Z",
            "2026-08-03T05:00:00Z",
            "2026-08-04T04:00:00Z",
            "2026-08-05T04:00:00Z",
        ]),
        "exit_timestamp": pd.to_datetime([
            "2026-08-03T04:30:00Z",
            "2026-08-03T05:30:00Z",
            "2026-08-04T04:30:00Z",
            "2026-08-05T04:30:00Z",
        ]),
        "pnl_points": [10.0, -2.0, -4.0, 8.0],
        "label": [1, 0, 0, 1],
    })

    class FakeTradeOutcomePipeline:
        def __init__(self, config, use_hybrid_transfer):
            pass

        def prepare_dataset(self, rows):
            return trade_df.copy()

        def run_walk_forward(self, frame, market_snapshots_df, n_splits):
            return None, {"mean_auc": 0.6, "leakage_guard_passed": True}

    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    monkeypatch.setattr("supabase.create_client", lambda url, key: object())
    monkeypatch.setattr(train_offline, "_fetch_ml_collection", lambda client: [])
    monkeypatch.setattr(train_offline, "_fetch_trade_exit_timestamps", lambda client: {})
    monkeypatch.setattr(train_offline, "TradeOutcomePipeline", FakeTradeOutcomePipeline)
    monkeypatch.setattr(
        "ml_signal.predictor.get_next_model_version_and_path",
        lambda models_dir: (str(tmp_path / "v999.joblib"), "v999"),
    )

    train_offline.main([
        "--pipeline", "trade_outcomes",
        "--no-hybrid",
        "--no-promote",
        "--metrics-path", str(metrics_path),
    ])

    summary = json.loads(metrics_path.read_text())
    assert summary["sharpe_status"] == "computed"
    assert summary["sharpe_trades"] == 4
    assert summary["sharpe_days"] == 3
    assert summary["sharpe_total_pnl_points"] == 12.0
    assert summary["sharpe_annualized"] == pytest.approx(9.165151)

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


def test_cv_metrics_preserve_auditable_fold_diagnostics():
    fold_info = {
        "fold": 2,
        "train_start": pd.Timestamp("2026-01-01T10:00:00Z"),
        "train_end": pd.Timestamp("2026-01-01T10:09:00Z"),
        "test_start": pd.Timestamp("2026-01-01T10:15:00Z"),
        "test_end": pd.Timestamp("2026-01-01T10:18:00Z"),
    }
    result = build_fold_result(
        fold_info,
        pd.Series([0, 0, 1, 1, 1]),
        pd.Series([0, 1, 1]),
        auc=0.75,
        brier=0.2,
    )

    metrics = compute_cv_metrics([result])

    assert metrics["fold_results"] == [{
        "fold": 2,
        "train_start": "2026-01-01T10:00:00+00:00",
        "train_end": "2026-01-01T10:09:00+00:00",
        "test_start": "2026-01-01T10:15:00+00:00",
        "test_end": "2026-01-01T10:18:00+00:00",
        "n_train": 5,
        "n_test": 3,
        "train_class_counts": {"negative": 2, "positive": 3},
        "test_class_counts": {"negative": 1, "positive": 2},
        "train_positive_rate": 0.6,
        "test_positive_rate": pytest.approx(2 / 3),
        "auc": 0.75,
        "brier": 0.2,
        "degenerate": False,
    }]
    # The diagnostics must be directly writable by every JSON report path.
    json.dumps(metrics, allow_nan=False)


def test_degenerate_fold_keeps_counts_and_chronological_bounds():
    result = build_fold_result(
        {"fold": 1, "test_start": pd.Timestamp("2026-01-01T10:00:00")},
        pd.Series([0, 1]),
        pd.Series([1, 1]),
        degenerate=True,
    )

    metrics = compute_cv_metrics([result])

    assert metrics["evaluable_folds"] == 0
    assert metrics["degenerate_folds"] == 1
    assert metrics["fold_results"][0]["n_train"] == 2
    assert metrics["fold_results"][0]["test_class_counts"] == {
        "negative": 0,
        "positive": 2,
    }

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


@pytest.mark.parametrize("field", ["mean_auc", "ci_95_lower", "min_fold_auc", "brier_score"])
@pytest.mark.parametrize("value", [None, np.nan, np.inf, "not-a-number"])
def test_evaluate_promotion_gate_rejects_non_finite_metrics(field, value):
    metrics = {
        "validation_method": "walk_forward_purged",
        "evaluable_folds": 4,
        "degenerate_folds": 0,
        "mean_auc": 0.56,
        "ci_95_lower": 0.51,
        "min_fold_auc": 0.42,
        "brier_score": 0.20,
        "leakage_guard_passed": True,
    }
    metrics[field] = value

    passed, reasons = evaluate_promotion_gate(metrics)

    assert passed is False
    assert reasons

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
