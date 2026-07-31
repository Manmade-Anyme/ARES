import pytest
import pandas as pd
from unittest.mock import MagicMock
from ml_signal.predictor import SignalPredictor

def test_signal_predictor_no_silent_zero_fill():
    # 1. Setup a mocked XGBoost model with synthetic feature names matching the fixed schema
    mock_model = MagicMock()
    mock_model.feature_names_in_ = [
        "candle_features__body_pct",
        "volume_features__vol_ratio",
        "oi_features__oi_bias"
    ]
    # Predict always returns a simple proba [0.0, 0.8] -> 80%
    import numpy as np
    mock_model.predict_proba.return_value = np.array([[0.0, 0.8]])
    
    predictor = SignalPredictor()
    predictor.model = mock_model
    predictor.feature_names = list(mock_model.feature_names_in_)

    # 2. Mock a realistic dictionary returned by build_feature_vector (post-fix)
    # The keys must include the prefixes.
    features = {
        "candle_features__body_pct": 10.5,
        "volume_features__vol_ratio": 2.1,
        "oi_features__oi_bias": -5.0,
        "meta_features__dte": 2.0  # Extra feature, should be dropped
    }
    
    # 3. Call predict_proba
    proba = predictor.predict_proba(features)
    
    # 4. Assert predict_proba was called with a DataFrame containing EXACTLY the correct data
    call_args = mock_model.predict_proba.call_args[0][0]
    assert isinstance(call_args, pd.DataFrame)
    
    # Assert columns match exactly what the model expects
    assert list(call_args.columns) == mock_model.feature_names_in_
    
    # Assert NO silent zero-filling occurred for the fields we provided
    assert call_args.iloc[0]["candle_features__body_pct"] == 10.5
    assert call_args.iloc[0]["volume_features__vol_ratio"] == 2.1
    assert call_args.iloc[0]["oi_features__oi_bias"] == -5.0
    
    # Output check
    assert proba == 0.8
