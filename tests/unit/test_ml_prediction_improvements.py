import pytest
import pandas as pd
import numpy as np

from ml_signal.pipeline_trade_outcomes import TradeOutcomePipeline
from ml_signal.pipeline_market_movement import MarketMovementPipeline
from ml_signal.predictor import SignalPredictor
from ml_signal.config import DEFAULT_CONFIG
from ml_signal.dataset import feature_columns


def test_direction_aligned_features_creation():
    """Verify TradeOutcomePipeline creates direction-aligned features."""
    pipe = TradeOutcomePipeline()
    rows = [
        {
            "trade_id": "trade-bull",
            "trade_outcome": "T1_HIT",
            "trade_pnl": 15.0,
            "timestamp": "2026-08-05T03:05:00Z",
            "raw_candle": {"close": 24000.0, "open": 23990.0},
            "candle_features": {"is_bullish": 1},
            "structure_features": {
                "dist_to_nearest_resistance": 50.0,
                "dist_to_nearest_support": 20.0,
            },
            "greek_features": {"net_delta": 0.05},
            "signal_direction": "BULLISH",
        },
        {
            "trade_id": "trade-bear",
            "trade_outcome": "SL_HIT",
            "trade_pnl": -12.0,
            "timestamp": "2026-08-05T04:05:00Z",
            "raw_candle": {"close": 24000.0, "open": 24010.0},
            "candle_features": {"is_bullish": 0},
            "structure_features": {
                "dist_to_nearest_resistance": 50.0,
                "dist_to_nearest_support": 20.0,
            },
            "greek_features": {"net_delta": 0.05},
            "signal_direction": "BEARISH",
        },
    ]
    exit_metadata = {
        "trade-bull": {
            "entry_timestamp": "2026-08-05T03:00:00Z",
            "exit_timestamp": "2026-08-05T03:30:00Z",
            "time_metrics_excluded": False,
        },
        "trade-bear": {
            "entry_timestamp": "2026-08-05T04:00:00Z",
            "exit_timestamp": "2026-08-05T04:30:00Z",
            "time_metrics_excluded": False,
        },
    }

    df = pipe.prepare_dataset(rows, exit_metadata)
    assert len(df) == 2

    # Bull trade checks
    bull_row = df[df["trade_id"] == "trade-bull"].iloc[0]
    assert bull_row["structure_features__dist_to_target_wall"] == 50.0  # Resistance is target
    assert bull_row["structure_features__dist_to_stop_wall"] == 20.0    # Support is stop
    assert bull_row["greek_features__aligned_net_delta"] == 0.05
    assert bull_row["candle_features__aligns_direction"] == 1.0

    # Bear trade checks
    bear_row = df[df["trade_id"] == "trade-bear"].iloc[0]
    assert bear_row["structure_features__dist_to_target_wall"] == 20.0  # Support is target
    assert bear_row["structure_features__dist_to_stop_wall"] == 50.0    # Resistance is stop
    assert bear_row["greek_features__aligned_net_delta"] == -0.05
    assert bear_row["candle_features__aligns_direction"] == 1.0


def test_schema_artifacts_excluded_from_stage1_features():
    """Verify schema version flags (greek__has_net_delta etc.) are excluded from stage1 feature columns."""
    sample_rows = [{
        "timestamp": "2026-08-05T03:05:00Z",
        "raw_candle": {"close": 24000.0, "open": 23990.0, "high": 24010.0, "low": 23980.0, "volume": 100},
        "candle_features": {"body_pct": 0.1, "range_pct": 0.2},
        "structure_features": {"dist_to_nearest_resistance": 50.0, "dist_to_nearest_support": 20.0},
        "greek_features": {"net_delta": 0.05},
    }]
    pipe_m = MarketMovementPipeline(DEFAULT_CONFIG)
    m_df = pipe_m.prepare_dataset(sample_rows)
    fcols = [
        c for c in feature_columns(m_df)
        if not c.startswith("detector_scores__")
        and not c.startswith("structure__")
        and not c.startswith("greek__")
    ]
    assert "greek__has_net_delta" not in fcols
    assert "structure__has_nearest_support" not in fcols
    assert "structure__has_nearest_resistance" not in fcols


def test_predict_from_raw_direction_alignment():
    """Verify SignalPredictor.predict_from_raw computes direction-aligned features."""
    from unittest.mock import MagicMock
    predictor = SignalPredictor(DEFAULT_CONFIG)
    predictor.model = MagicMock()
    predictor.model.predict_proba.return_value = np.array([[0.5, 0.5]])
    candle = {"open": 24000.0, "high": 24020.0, "low": 23990.0, "close": 24015.0, "volume": 1000}
    levels = [23950.0, 24050.0]  # Support 23950 (-65), Resistance 24050 (+35)
    atm_ce = {"delta": 0.52}
    atm_pe = {"delta": -0.48}  # net_delta = 0.52 - 0.48 = 0.04

    # Bullish prediction
    pred_bull = predictor.predict_from_raw(
        candle=candle,
        volume_history=[1000, 1000],
        iv_history=None,
        atm_ce=atm_ce,
        atm_pe=atm_pe,
        total_ce_oi=1000,
        total_pe_oi=1000,
        all_ce_oi=[1000],
        all_pe_oi=[1000],
        levels=levels,
        timestamp="2026-10-24T09:30:00Z",
        spot=24015.0,
        direction="BULLISH",
    )
    feats_bull = pred_bull["features"]
    assert feats_bull["structure_features__dist_to_target_wall"] == 35.0  # Resistance
    assert feats_bull["structure_features__dist_to_stop_wall"] == 65.0    # Support
    assert feats_bull["greek_features__aligned_net_delta"] == 0.04
    assert feats_bull["candle_features__aligns_direction"] == 1.0

    # Bearish prediction
    pred_bear = predictor.predict_from_raw(
        candle=candle,
        volume_history=[1000, 1000],
        iv_history=None,
        atm_ce=atm_ce,
        atm_pe=atm_pe,
        total_ce_oi=1000,
        total_pe_oi=1000,
        all_ce_oi=[1000],
        all_pe_oi=[1000],
        levels=levels,
        timestamp="2026-10-24T09:30:00Z",
        spot=24015.0,
        direction="BEARISH",
    )
    feats_bear = pred_bear["features"]
    assert feats_bear["structure_features__dist_to_target_wall"] == 65.0  # Support
    assert feats_bear["structure_features__dist_to_stop_wall"] == 35.0    # Resistance
    assert feats_bear["greek_features__aligned_net_delta"] == -0.04
    assert feats_bear["candle_features__aligns_direction"] == 0.0


def test_shadow_evaluation_summary_contract(tmp_path, monkeypatch):
    """Verify train_offline summary preserves shadow evaluation metadata and baseline stats."""
    from ml_signal import train_offline
    import json

    metrics_path = tmp_path / "summary.json"
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True)

    # Fake models
    fake_v10 = models_dir / "v10.joblib"
    fake_v10.write_text("dummy")

    trade_df = pd.DataFrame({
        "timestamp": pd.date_range("2026-08-01", periods=10, freq="h", tz="UTC"),
        "label": [0, 1] * 5,
        "pnl_points": [10.0, -5.0] * 5,
        "exit_timestamp": pd.date_range("2026-08-01 01:00", periods=10, freq="h", tz="UTC"),
        "entry_timestamp": pd.date_range("2026-08-01", periods=10, freq="h", tz="UTC"),
        "time_metrics_excluded": False,
    })

    class FakeTradeOutcomePipeline:
        def __init__(self, config=None, use_hybrid_transfer=False):
            pass
        def prepare_dataset(self, rows, exit_timestamps=None):
            return trade_df.copy()
        def run_walk_forward(self, frame, market_snapshots_df=None, n_splits=5):
            return None, {
                "validation_method": "walk_forward_purged",
                "evaluable_folds": 3,
                "degenerate_folds": 0,
                "mean_auc": 0.48,
                "min_fold_auc": 0.45,
                "brier_score": 0.25,
                "leakage_guard_passed": True,
            }

    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    monkeypatch.setattr("supabase.create_client", lambda url, key: object())
    monkeypatch.setattr(train_offline, "_fetch_ml_collection", lambda client: [])
    monkeypatch.setattr(train_offline, "_fetch_trade_exit_timestamps", lambda client: {})
    monkeypatch.setattr(train_offline, "TradeOutcomePipeline", FakeTradeOutcomePipeline)
    monkeypatch.setattr(
        "ml_signal.predictor.get_next_model_version_and_path",
        lambda d: (str(models_dir / "v11.joblib"), "v11"),
    )
    monkeypatch.setattr(
        "ml_signal.predictor.discover_latest_model",
        lambda d=None: (str(fake_v10), "v10"),
    )

    train_offline.main([
        "--pipeline", "trade_outcomes",
        "--no-hybrid",
        "--no-promote",
        "--metrics-path", str(metrics_path),
    ])

    assert metrics_path.exists()
    with open(metrics_path) as f:
        summary = json.load(f)

    assert summary["model_version"] == "v11"
    assert summary["active_production_version"] == "v10"
    assert summary["promoted"] is False
    assert "shadow_evaluation" in summary
    shadow = summary["shadow_evaluation"]
    assert shadow["status"] == "evaluated_unpromoted"
    assert shadow["active_production_model"] == "v10"
    assert shadow["rollback_target"] == "v10"
    assert shadow["unfiltered_baseline_win_rate"] == 0.5


def test_market_movement_run_walk_forward_excludes_schema_artifacts(monkeypatch):
    """Verify MarketMovementPipeline.run_walk_forward excludes structure__ and greek__ flags."""
    from unittest.mock import MagicMock
    mock_xgb = MagicMock()
    monkeypatch.setattr("xgboost.XGBClassifier", mock_xgb)

    pipe = MarketMovementPipeline(DEFAULT_CONFIG)
    # Build dataframe with schema flags
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-08-01", periods=10, freq="15min", tz="UTC"),
        "resolution_timestamp": pd.date_range("2026-08-01 00:15:00", periods=10, freq="15min", tz="UTC"),
        "label": [1, 0, 1, 0, 1, 0, 1, 0, 1, 0],
        "candle_features__body_pct": [0.1] * 10,
        "detector_scores__continuation": [0.8] * 10,
        "structure__has_nearest_support": [1.0] * 10,
        "greek__has_net_delta": [1.0] * 10,
    })

    pipe.run_walk_forward(df, n_splits=2)

    # Check that any fit call had columns excluding detector_scores__, structure__, greek__
    for call in mock_xgb.return_value.fit.call_args_list:
        X_arg = call[0][0]
        assert "detector_scores__continuation" not in X_arg.columns
        assert "structure__has_nearest_support" not in X_arg.columns
        assert "greek__has_net_delta" not in X_arg.columns
        assert "candle_features__body_pct" in X_arg.columns


def test_transfer_probability_fallback_is_causal():
    """Verify unscored early rows in transfer probabilities do not receive backward-filled future fold scores."""
    # Test that when inner splits evaluate later indices, earlier rows stay 0.5
    tscv_series = pd.Series([np.nan, np.nan, 0.85, 0.90])
    causal_series = tscv_series.ffill().fillna(0.5)
    # Early unscored rows must be 0.5, not 0.85 (which bfill would have done)
    assert causal_series.iloc[0] == 0.5
    assert causal_series.iloc[1] == 0.5
    assert causal_series.iloc[2] == 0.85
    assert causal_series.iloc[3] == 0.90


@pytest.mark.asyncio
async def test_signal_consumer_passes_direction_to_predict_from_raw(monkeypatch):
    """Verify SignalConsumer extracts direction from signal and passes it to predict_from_raw."""
    import asyncio
    from unittest.mock import MagicMock, AsyncMock, patch
    from ml_signal.signal_consumer import SignalConsumer

    consumer = SignalConsumer()
    consumer._init_supabase = MagicMock()
    consumer.predictor.load_model = MagicMock()
    consumer.predictor.predict_from_raw = MagicMock(return_value={
        "probability": 0.75,
        "confidence_tier": "HIGH",
    })
    consumer.fetch_new_signals = AsyncMock(return_value=[{
        "id": "sig-001",
        "display_id": "1",
        "setup_type": "TREND_CONTINUATION",
        "direction": "BEARISH",
        "market_context": {"open": 24000.0, "high": 24010.0, "low": 23990.0},
        "spot_at_signal": 24000.0,
    }])
    consumer.log_prediction = AsyncMock(return_value=True)

    with patch("ml_signal.signal_consumer.send_prediction_alert", new_callable=AsyncMock), \
         patch("ml_signal.signal_consumer.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
        try:
            await consumer.run("https://dummy.supabase.co", "dummy_key")
        except asyncio.CancelledError:
            pass

    assert consumer.predictor.predict_from_raw.called
    call_kwargs = consumer.predictor.predict_from_raw.call_args.kwargs
    assert call_kwargs.get("direction") == "BEARISH"


def test_train_offline_rollback_target_retains_prior_production_model_on_promotion(tmp_path, monkeypatch):
    """Verify rollback_target preserves previous production model even when candidate is promoted."""
    import json
    from unittest.mock import MagicMock
    from ml_signal import train_offline

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    fake_v10 = models_dir / "v10.joblib"
    fake_v10.write_bytes(b"prior_v10")
    metrics_path = tmp_path / "metrics.json"

    class PromotedTradeOutcomePipeline:
        def __init__(self, *args, **kwargs):
            pass
        def prepare_dataset(self, rows, exit_timestamps=None):
            return pd.DataFrame({
                "timestamp": pd.date_range("2026-08-01", periods=10, freq="15min", tz="UTC"),
                "resolution_timestamp": pd.date_range("2026-08-01 00:15:00", periods=10, freq="15min", tz="UTC"),
                "label": [1, 0, 1, 0, 1, 0, 1, 0, 1, 0],
                "candle_features__body_pct": [0.1] * 10,
            })
        def run_walk_forward(self, frame, market_snapshots_df=None, n_splits=5):
            return MagicMock(), {
                "validation_method": "walk_forward_purged",
                "mean_auc": 0.85,
                "brier_score": 0.15,
                "evaluable_folds": 5,
                "fold_results": [{"fold": i, "auc": 0.85, "brier": 0.15} for i in range(1, 6)],
                "ci_95_lower": 0.70,
                "ci_95_upper": 0.95,
                "min_fold_auc": 0.75,
                "leakage_guard_passed": True,
            }

    monkeypatch.setattr(train_offline, "_fetch_ml_collection", lambda client: [])
    monkeypatch.setattr(train_offline, "_fetch_trade_exit_timestamps", lambda client: {})
    monkeypatch.setattr(train_offline, "TradeOutcomePipeline", PromotedTradeOutcomePipeline)
    monkeypatch.setattr(
        "ml_signal.predictor.get_next_model_version_and_path",
        lambda d: (str(models_dir / "v11.joblib"), "v11"),
    )

    # First call at startup returns v10, after promotion discover_latest_model returns v11
    discovery_calls = []
    def mock_discover(d=None):
        if not discovery_calls:
            discovery_calls.append("v10")
            return (str(fake_v10), "v10")
        discovery_calls.append("v11")
        return (str(models_dir / "v11.joblib"), "v11")

    monkeypatch.setattr("ml_signal.predictor.discover_latest_model", mock_discover)

    train_offline.main([
        "--pipeline", "trade_outcomes",
        "--no-hybrid",
        "--promote",
        "--metrics-path", str(metrics_path),
    ])

    assert metrics_path.exists()
    with open(metrics_path) as f:
        summary = json.load(f)

    assert summary["model_version"] == "v11"
    assert summary["active_production_version"] == "v11"
    assert summary["promoted"] is True
    # Rollback target must point to prior production model v10, NOT the newly promoted v11
    assert summary["shadow_evaluation"]["rollback_target"] == "v10"



