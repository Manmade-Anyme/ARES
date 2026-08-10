"""
TASK-208 — Tests for Dynamic ML Model Auto-Versioning and Discovery.

Verifies that:
1. discover_latest_model() correctly identifies the highest numerical version (v1 -> v2 -> v10).
2. get_next_model_version_and_path() increments the version for retraining.
3. SignalPredictor loads the latest discovered model and emits its version in predictions.
4. Discord alerts dynamically display the loaded model version (e.g. v2.joblib / v2).
"""
import os
import re
import datetime
import pytest
from unittest.mock import MagicMock, patch
import numpy as np

from ml_signal.predictor import (
    SignalPredictor,
    discover_latest_model,
    get_next_model_version_and_path,
)
import alerts


def test_discover_latest_model_numeric_ordering(tmp_path):
    """discover_latest_model should return highest numerical version, not lexical."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()

    # Empty dir fallback
    path, ver = discover_latest_model(models_dir)
    assert ver == "v1"
    assert path.endswith("v1.joblib")

    # Create v1.joblib and v2.joblib
    (models_dir / "v1.joblib").touch()
    path, ver = discover_latest_model(models_dir)
    assert ver == "v1"
    assert path.endswith("v1.joblib")

    (models_dir / "v2.joblib").touch()
    path, ver = discover_latest_model(models_dir)
    assert ver == "v2"
    assert path.endswith("v2.joblib")

    # Create v10.joblib (should beat v2 lexically and numerically)
    (models_dir / "v10.joblib").touch()
    path, ver = discover_latest_model(models_dir)
    assert ver == "v10"
    assert path.endswith("v10.joblib")


def test_get_next_model_version_and_path(tmp_path):
    """get_next_model_version_and_path should compute v{max+1}."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()

    # Empty dir -> v1
    path, ver = get_next_model_version_and_path(models_dir)
    assert ver == "v1"
    assert path.endswith("v1.joblib")

    # When v1 exists -> v2
    (models_dir / "v1.joblib").touch()
    path, ver = get_next_model_version_and_path(models_dir)
    assert ver == "v2"
    assert path.endswith("v2.joblib")

    # When v2 exists -> v3
    (models_dir / "v2.joblib").touch()
    path, ver = get_next_model_version_and_path(models_dir)
    assert ver == "v3"
    assert path.endswith("v3.joblib")


def test_signal_predictor_auto_version_tagging(tmp_path):
    """SignalPredictor should record loaded version and include it in predictions."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "v2.joblib").touch()

    mock_model = MagicMock()
    mock_model.feature_names_in_ = ["candle_features__body_pct"]
    mock_model.predict_proba.return_value = np.array([[0.1, 0.9]])

    with patch("joblib.load", return_value=mock_model):
        predictor = SignalPredictor()
        predictor.load_model(str(models_dir / "v2.joblib"))

        assert predictor.loaded_model_version == "v2"
        assert predictor.model_filename == "v2.joblib"

        pred = predictor.predict_from_raw(
            candle={"open": 100, "high": 110, "low": 90, "close": 105, "volume": 1000},
            volume_history=[1000],
            iv_history=[12.0],
            atm_ce={"oi": 1000, "oi_change_pct": 5.0},
            atm_pe={"oi": 2000, "oi_change_pct": 10.0},
            total_ce_oi=5000,
            total_pe_oi=6000,
            all_ce_oi=[1000],
            all_pe_oi=[2000],
            levels=[110, 90],
            timestamp=datetime.datetime(2026, 7, 30, 10, 30),
            spot=105,
        )
        assert pred["model_version"] == "v2"
        assert pred["probability"] == 0.9


@pytest.mark.asyncio
async def test_discord_startup_and_signal_alert_version_display():
    """Startup alert and signal alert should reflect active model version."""
    from unittest.mock import AsyncMock

    # 1. Startup alert with v2.joblib
    with patch.object(alerts.settings, "discord_webhook_url", "http://discord.invalid"), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.raise_for_status = MagicMock()

        await alerts.send_startup_alert(
            pdh=24100.0,
            pdl=23900.0,
            profile_name="DEFAULT",
            ml_active=True,
            predictor_active=True,
            predictor_model_name="v2.joblib",
        )
        payload = mock_post.call_args[1]["json"]
        assert "+ [+] ML Predictor : ACTIVE (v2.joblib)" in payload["content"]

    # 2. Signal alert with v2 model version
    signal = MagicMock()
    signal.direction.value = "BULLISH"
    signal.setup_type.value = "EXHAUSTION_REVERSAL"
    signal.confidence = "HIGH"
    signal.entry_zone = (24000.0, 24010.0)
    signal.stop_loss = 23985.0
    signal.target_1 = 24035.0
    signal.target_2 = 24070.0
    signal.strike_to_trade = 24000
    signal.option_type = "CE"
    signal.reasons = ["RSI Divergence"]
    signal.suggested_lots = None
    signal.ml_prediction = {"probability": 0.85, "model_version": "v2"}

    with patch.object(alerts.settings, "discord_webhook_url", "http://discord.invalid"), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.raise_for_status = MagicMock()

        await alerts.send_discord(signal, spot=24005.0)
        payload = mock_post.call_args[1]["json"]
        fields = payload["embeds"][0]["fields"]
        ml_field = next(f for f in fields if f["name"] == "🤖 ML Prediction")
        assert "**85%** (proxy model, v2)" in ml_field["value"]


def test_gitignore_does_not_ignore_versioned_models():
    """Verify that .gitignore does not ignore ml_signal/models/*.joblib so retrained models are tracked."""
    import subprocess
    result = subprocess.run(
        ["git", "check-ignore", "-v", "ml_signal/models/v2.joblib"],
        capture_output=True,
        text=True,
    )
    # git check-ignore exits with 1 when the file is NOT ignored (what we want)
    assert result.returncode == 1, f"ml_signal/models/v2.joblib is ignored: {result.stdout}"
