import pytest
import pandas as pd
from unittest.mock import MagicMock
from ml_signal.predictor import SignalPredictor, HybridPredictorBundle

def test_hybrid_predictor_bundle_prediction():
    # Mock Stage 1 and Stage 2 models
    stage1_mock = MagicMock()
    # predict_proba returns a 2D array, we want the [:, 1] slice to be say 0.85
    stage1_mock.predict_proba.return_value = [[0.15, 0.85]]
    
    stage2_mock = MagicMock()
    # Stage 2 predict_proba returns 0.95
    stage2_mock.predict_proba.return_value = [[0.05, 0.95]]
    
    bundle = HybridPredictorBundle(
        stage1_model=stage1_mock,
        stage2_model=stage2_mock,
        stage1_feature_names=["f1", "f2"],
        stage2_feature_names=["meta_features__market_movement_prob", "f3"],
        model_version="v10",
        created_at="now",
        metrics_summary={}
    )
    
    predictor = SignalPredictor()
    predictor.model = bundle
    predictor.loaded_model_version = "v10"
    
    # Mock build_feature_vector since predict_from_raw calls it
    # We will just patch predict_from_raw's internal call to build_feature_vector or test predict_from_raw with a patch
    import ml_signal.predictor as pred_module
    original_build = pred_module.build_feature_vector
    try:
        pred_module.build_feature_vector = MagicMock(return_value={"f1": 1.0, "f2": 2.0, "f3": 3.0})
        
        result = predictor.predict_from_raw(
            candle={},
            volume_history=[],
            iv_history=[],
            atm_ce={},
            atm_pe={},
            total_ce_oi=0,
            total_pe_oi=0,
            all_ce_oi=[],
            all_pe_oi=[],
            levels=[],
            timestamp="2026-01-01",
            spot=100.0,
        )
        
        assert result["probability"] == 0.9500
        assert result["model_version"] == "v10"
        
        # Verify stage 1 was called with pure market features
        df1_args = stage1_mock.predict_proba.call_args[0][0]
        assert list(df1_args.columns) == ["f1", "f2"]
        
        # Verify stage 2 was called with the transfer feature and structural feature
        df2_args = stage2_mock.predict_proba.call_args[0][0]
        assert list(df2_args.columns) == ["meta_features__market_movement_prob", "f3"]
        assert df2_args.iloc[0]["meta_features__market_movement_prob"] == 0.85
        assert df2_args.iloc[0]["f3"] == 3.0
        
    finally:
        pred_module.build_feature_vector = original_build
