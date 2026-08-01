import json
from typing import Optional, Dict, List, Any

import joblib
import numpy as np
import pandas as pd

from .config import MLConfig, DEFAULT_CONFIG
from .features import build_feature_vector


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
        self.training_info = None

    def training_summary(self) -> str:
        """One line describing the offline data this model was fitted on.

        Read from the training report rather than hardcoded, so it cannot drift
        when the model is retrained. Returns "provenance unknown" when no report
        sits alongside the model — which is itself worth seeing in the banner,
        since it means the live model predates provenance tracking.
        """
        info = self.training_info
        if not info:
            return "provenance unknown"

        bits = []
        source = info.get("label_source")
        if source:
            bits.append(str(source))
        if info.get("n_samples") is not None:
            bits.append(f"n={info['n_samples']}")
        if info.get("auc_roc") is not None:
            bits.append(f"AUC {float(info['auc_roc']):.3f}")
        if info.get("provisional"):
            bits.append("PROVISIONAL")
        if info.get("trained_at"):
            bits.append(str(info["trained_at"])[:10])
        return " · ".join(bits) if bits else "provenance unknown"

    def load_model(self, path: Optional[str] = None) -> None:
        """Load the joblib model, its column order, and its training provenance.

        Raises whatever joblib/xgboost raise; main.py treats a failure here as
        "predictor unavailable" and carries on without one. A missing or
        unreadable report is NOT fatal — the model still serves, it just reports
        its provenance as unknown.
        """
        model_path = path or self.config.model_path
        self.model = joblib.load(model_path)

        if hasattr(self.model, "feature_names_in_"):
            self.feature_names = list(self.model.feature_names_in_)
        elif hasattr(self.model, "estimator") and hasattr(self.model.estimator, "feature_names_in_"):
            self.feature_names = list(self.model.estimator.feature_names_in_)
        else:
            self.feature_names = None

        self.training_info = self._load_training_report()

    def _load_training_report(self) -> Optional[Dict[str, Any]]:
        """The train_offline metrics for this model, or None if unavailable."""
        path = getattr(self.config, "model_report_path", None)
        if not path:
            return None
        try:
            with open(path) as fh:
                report = json.load(fh)
            return report if isinstance(report, dict) else None
        except (OSError, ValueError):
            # Report missing or malformed — never block serving on it.
            return None

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
            "model_version": self.config.active_model_version,
            "spot": spot,
            "timestamp": str(timestamp),
            "features": {k: round(v, 4) if isinstance(v, float) else v for k, v in features.items()},
        }
