import os
import re
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple

import joblib
import numpy as np
import pandas as pd

from .config import MLConfig, DEFAULT_CONFIG
from .features import build_feature_vector


def discover_latest_model(models_dir: Optional[str | Path] = None) -> Tuple[str, str]:
    """Finds the highest numbered version model file in models_dir (e.g. 'v1.joblib', 'v2.joblib').

    Returns (model_path, version_string), e.g. ('ml_signal/models/v1.joblib', 'v1').
    Falls back to ('ml_signal/models/v1.joblib', 'v1') if no model files exist.
    """
    if models_dir is None:
        target_dir = Path(__file__).parent / "models"
    else:
        target_dir = Path(models_dir)

    if not target_dir.exists():
        return str(target_dir / "v1.joblib"), "v1"

    pattern = re.compile(r"^v(\d+)\.joblib$")
    highest_ver = 0
    highest_file = None

    for file in target_dir.iterdir():
        if file.is_file():
            match = pattern.match(file.name)
            if match:
                ver = int(match.group(1))
                if ver > highest_ver:
                    highest_ver = ver
                    highest_file = file

    if highest_file is not None:
        return str(highest_file), f"v{highest_ver}"
    return str(target_dir / "v1.joblib"), "v1"


def get_next_model_version_and_path(models_dir: Optional[str | Path] = None) -> Tuple[str, str]:
    """Determines the next version string and save path for a newly trained model.

    E.g., if 'v1.joblib' exists, returns ('ml_signal/models/v2.joblib', 'v2').
    """
    if models_dir is None:
        target_dir = Path(__file__).parent / "models"
    else:
        target_dir = Path(models_dir)

    target_dir.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(r"^v(\d+)\.joblib$")
    highest_ver = 0

    for file in target_dir.iterdir():
        if file.is_file():
            match = pattern.match(file.name)
            if match:
                ver = int(match.group(1))
                if ver > highest_ver:
                    highest_ver = ver

    next_ver = highest_ver + 1
    return str(target_dir / f"v{next_ver}.joblib"), f"v{next_ver}"


class SignalPredictor:
    """Scores a fired signal with the offline-trained reliability model.

    Read-only with respect to trading: the probability is attached to the signal
    for display in the Discord alert and gates nothing.
    """

    def __init__(self, config: MLConfig = DEFAULT_CONFIG):
        """Hold config only — no model is read until load_model is called."""
        self.config = config
        self.model = None
        self.feature_names = None
        self.loaded_model_path: Optional[str] = None
        self.loaded_model_version: str = config.active_model_version

    @property
    def model_filename(self) -> str:
        """Returns the base filename of the currently loaded model (e.g. 'v1.joblib')."""
        if self.loaded_model_path:
            return os.path.basename(self.loaded_model_path)
        return f"{self.loaded_model_version}.joblib"

    def load_model(self, path: Optional[str] = None) -> None:
        """Load the joblib model and remember the column order it was fit on.

        If path is None, automatically discovers and loads the highest-versioned
        model in the models directory. Raises whatever joblib/xgboost raise;
        main.py treats a failure here as "predictor unavailable" and carries on.
        """
        if path is None:
            model_path, version = discover_latest_model()
            self.loaded_model_version = version
        else:
            model_path = path
            match = re.search(r"v(\d+)\.joblib", os.path.basename(path))
            self.loaded_model_version = f"v{match.group(1)}" if match else self.config.active_model_version

        self.loaded_model_path = model_path
        self.model = joblib.load(model_path)

        if hasattr(self.model, "feature_names_in_"):
            self.feature_names = list(self.model.feature_names_in_)
        elif hasattr(self.model, "estimator") and hasattr(self.model.estimator, "feature_names_in_"):
            self.feature_names = list(self.model.estimator.feature_names_in_)
        else:
            self.feature_names = None

    def predict_proba(self, features: Dict[str, float]) -> float:
        """Probability of the positive class for one feature dict.

        Reindexes to the model's training column order; a feature the model was
        not fit on is dropped and one it expects but did not receive is missing.
        """
        if self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        df = pd.DataFrame([features])

        if self.feature_names is not None:
            for col in self.feature_names:
                if col not in df.columns:
                    df[col] = None
            df = df[self.feature_names]

        # Coerce to float64 so an unknown feature arrives as NaN, which XGBoost
        # treats natively as missing.
        #
        # compute_structure_features returns None when a distance is unknown (no
        # level above spot, no prior-day high). Across many rows pandas infers a
        # float column; on the SINGLE row built here the column stays object
        # dtype and predict_proba raises "DataFrame.dtypes for data must be int,
        # float, bool or category". 668 of the last 1000 collected snapshots
        # carry at least one such None, so this raised on most live signals.
        #
        # NaN, not 0.0: a zero distance means "spot is exactly at the level",
        # which is a real and strongly-signalling market state, not "unknown".
        df = df.astype("float64")

        proba = self.model.predict_proba(df)[0, 1]
        return float(proba)

    def classify_confidence(self, proba: float) -> str:
        """Bucket a probability into HIGH / MEDIUM / LOW on the configured thresholds."""
        if proba >= self.config.high_threshold:
            return "HIGH"
        elif proba >= self.config.medium_threshold:
            return "MEDIUM"
        else:
            return "LOW"

    def predict_from_raw(
        self,
        candle: Dict[str, float],
        volume_history: List[int],
        iv_history: Optional[List[float]],
        atm_ce: Dict[str, Any],
        atm_pe: Dict[str, Any],
        total_ce_oi: int,
        total_pe_oi: int,
        all_ce_oi: Optional[List[int]],
        all_pe_oi: Optional[List[int]],
        levels: List[float],
        timestamp,
        spot: float,
        pdh: Optional[float] = None,
        pdl: Optional[float] = None,
        dte: Optional[int] = None,
        is_expiry: bool = False,
    ) -> Dict[str, Any]:
        """Build the feature vector from a raw market snapshot and score it.

        Takes the same inputs MLCollector.snapshot does, so the live prediction
        and the stored training row are computed by the identical code path.
        Returns probability, confidence tier, model version and the features
        used — the dict main.py attaches to the signal as `ml_prediction`.
        """
        features = build_feature_vector(
            candle=candle,
            volume_history=volume_history,
            iv_history=iv_history,
            atm_ce=atm_ce,
            atm_pe=atm_pe,
            total_ce_oi=total_ce_oi,
            total_pe_oi=total_pe_oi,
            all_ce_oi=all_ce_oi,
            all_pe_oi=all_pe_oi,
            levels=levels,
            timestamp=timestamp,
            spot=spot,
            pdh=pdh,
            pdl=pdl,
            dte=dte,
            is_expiry=is_expiry,
            config=self.config,
        )

        proba = self.predict_proba(features)
        confidence = self.classify_confidence(proba)

        return {
            "probability": round(proba, 4),
            "confidence_tier": confidence,
            "model_version": self.loaded_model_version or self.config.active_model_version,
            "spot": spot,
            "timestamp": str(timestamp),
            "features": {k: round(v, 4) if isinstance(v, float) else v for k, v in features.items()},
        }
