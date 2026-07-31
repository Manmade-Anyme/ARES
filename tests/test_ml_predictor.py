import pytest
import pandas as pd
import datetime
from unittest.mock import MagicMock
from ml_signal.predictor import SignalPredictor

def test_signal_predictor_predict_from_raw():
    # 1. Setup a mocked XGBoost model with synthetic feature names matching the fixed schema
    mock_model = MagicMock()
    mock_model.feature_names_in_ = [
        "candle_features__body_pct",
        "oi_features__oi_bias"
    ]
    # Predict always returns a simple proba [0.0, 0.8] -> 80%
    import numpy as np
    mock_model.predict_proba.return_value = np.array([[0.0, 0.8]])
    
    predictor = SignalPredictor()
    predictor.model = mock_model
    predictor.feature_names = list(mock_model.feature_names_in_)

    # 3. Call predict_from_raw
    proba = predictor.predict_from_raw(
        candle={"open": 100, "high": 110, "low": 90, "close": 105, "volume": 1000},
        volume_history=[900, 950, 1000],
        iv_history=[12.0, 12.5],
        atm_ce={"oi": 1000, "oi_change_pct": 5.0},
        atm_pe={"oi": 2000, "oi_change_pct": 10.0},
        total_ce_oi=5000,
        total_pe_oi=6000,
        all_ce_oi=[1000],
        all_pe_oi=[2000],
        levels=[110, 90],
        timestamp=datetime.datetime(2026, 7, 30, 10, 30),
        spot=105,
        pdh=110,
        pdl=90
    )
    
    # 4. Assert predict_proba was called with a DataFrame containing EXACTLY the correct data
    call_args = mock_model.predict_proba.call_args[0][0]
    assert isinstance(call_args, pd.DataFrame)
    
    # Assert columns match exactly what the model expects
    assert list(call_args.columns) == mock_model.feature_names_in_
    
    # Compute what the values should be based on `compute_candle_features` and `compute_oi_features`
    expected_body_pct = (105 - 100) / 105 * 100
    expected_oi_bias = 5.0 - 10.0
    
    assert abs(call_args.iloc[0]["candle_features__body_pct"] - expected_body_pct) < 1e-6
    assert abs(call_args.iloc[0]["oi_features__oi_bias"] - expected_oi_bias) < 1e-6
    
    # Output check
    assert proba["probability"] == 0.8
