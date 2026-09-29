"""Post-hoc probability calibration with monotonicity and Brier safeguards."""
from typing import Any, Optional, Tuple

import numpy as np
from sklearn.base import clone
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import TimeSeriesSplit


class ProbabilityCalibrator:
    """Fit isotonic or Platt calibration on out-of-fold model probabilities."""

    def __init__(self, method: str = "isotonic", min_brier_improvement: float = 0.0):
        self.method = method
        self.min_brier_improvement = min_brier_improvement
        self.model = None
        self.is_calibrated = False
        self.rejection_reason: Optional[str] = None
        self.brier_before: Optional[float] = None
        self.brier_after: Optional[float] = None

    def fit(self, y_true, y_prob, method: Optional[str] = None):
        actual = np.asarray(y_true, dtype=float).reshape(-1)
        probability = np.asarray(y_prob, dtype=float).reshape(-1)
        if actual.size < 2 or actual.size != probability.size:
            raise ValueError("calibration requires at least two aligned observations")
        if not np.isfinite(actual).all() or not np.isfinite(probability).all():
            raise ValueError("calibration inputs must be finite")
        if not np.isin(actual, [0.0, 1.0]).all() or ((probability < 0) | (probability > 1)).any():
            raise ValueError("labels must be binary and probabilities must be in [0, 1]")
        if len(np.unique(actual)) < 2:
            raise ValueError("calibration labels must contain both classes")
        method = method or self.method
        if method not in {"isotonic", "platt"}:
            raise ValueError("method must be 'isotonic' or 'platt'")

        if method == "isotonic":
            estimator = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
            estimator.fit(probability, actual)
            calibrated = estimator.predict(probability)
            grid_output = estimator.predict(np.linspace(0.0, 1.0, 501))
        else:
            clipped = np.clip(probability, 1e-6, 1.0 - 1e-6)
            logits = np.log(clipped / (1.0 - clipped)).reshape(-1, 1)
            estimator = LogisticRegression(solver="lbfgs", max_iter=1000)
            estimator.fit(logits, actual)
            if float(estimator.coef_[0, 0]) < 0.0:
                self.model = None
                self.is_calibrated = False
                self.rejection_reason = "non_monotonic_fit"
                raise ValueError("calibrator fit is not monotonically increasing")
            calibrated = estimator.predict_proba(logits)[:, 1]
            grid = np.clip(np.linspace(0.0, 1.0, 501), 1e-6, 1.0 - 1e-6)
            grid_logits = np.log(grid / (1.0 - grid)).reshape(-1, 1)
            grid_output = estimator.predict_proba(grid_logits)[:, 1]

        if np.any(np.diff(grid_output) < -1e-10):
            self.model = None
            self.is_calibrated = False
            self.rejection_reason = "non_monotonic_fit"
            raise ValueError("calibrator fit is not monotonically increasing")
        self.brier_before = float(np.mean((probability - actual) ** 2))
        self.brier_after = float(np.mean((calibrated - actual) ** 2))
        improvement = self.brier_before - self.brier_after
        if improvement <= self.min_brier_improvement:
            self.model = None
            self.is_calibrated = False
            self.rejection_reason = "no_brier_improvement"
            return self
        self.method = method
        self.model = estimator
        self.is_calibrated = True
        self.rejection_reason = None
        return self

    def fit_cross_validated(self, estimator, X, y, n_splits: int = 5, method: Optional[str] = None):
        """Build chronological OOF probabilities, then fit the post-hoc mapping."""
        target = np.asarray(y, dtype=int).reshape(-1)
        if len(target) != len(X) or len(target) < 4:
            raise ValueError("cross-validation requires at least four aligned rows")
        splitter = TimeSeriesSplit(n_splits=min(n_splits, len(target) - 2))
        oof_indices, oof_probabilities = [], []
        for train_idx, valid_idx in splitter.split(X):
            if len(np.unique(target[train_idx])) < 2:
                continue
            fitted = clone(estimator)
            train_x = X.iloc[train_idx] if hasattr(X, "iloc") else X[train_idx]
            train_y = target[train_idx]
            params = fitted.get_params(deep=False) if hasattr(fitted, "get_params") else {}
            if params.get("early_stopping_rounds") and len(train_idx) >= 5:
                split_at = max(2, int(len(train_idx) * 0.8))
                fit_x = train_x.iloc[:split_at] if hasattr(train_x, "iloc") else train_x[:split_at]
                fit_y = train_y[:split_at]
                valid_x = train_x.iloc[split_at:] if hasattr(train_x, "iloc") else train_x[split_at:]
                valid_y = train_y[split_at:]
                if len(np.unique(fit_y)) == 2 and len(valid_y):
                    fitted.fit(fit_x, fit_y, eval_set=[(valid_x, valid_y)], verbose=False)
                else:
                    fitted.set_params(early_stopping_rounds=None)
                    fitted.fit(train_x, train_y)
            else:
                fitted.fit(train_x, train_y)
            valid_x = X.iloc[valid_idx] if hasattr(X, "iloc") else X[valid_idx]
            oof_indices.extend(valid_idx.tolist())
            oof_probabilities.extend(fitted.predict_proba(valid_x)[:, 1].tolist())
        if len(oof_indices) < 2:
            raise ValueError("not enough valid out-of-fold predictions to fit calibrator")
        self.fit(target[oof_indices], oof_probabilities, method=method)
        return self, np.asarray(oof_indices), np.asarray(oof_probabilities)

    def transform(self, y_prob):
        probability = np.asarray(y_prob, dtype=float)
        if not np.isfinite(probability).all() or ((probability < 0) | (probability > 1)).any():
            raise ValueError("probabilities must be finite and in [0, 1]")
        if not self.is_calibrated or self.model is None:
            return probability
        if self.method == "isotonic":
            return np.asarray(self.model.predict(probability), dtype=float)
        clipped = np.clip(probability, 1e-6, 1.0 - 1e-6)
        logits = np.log(clipped / (1.0 - clipped)).reshape(-1, 1)
        return np.asarray(self.model.predict_proba(logits)[:, 1], dtype=float)

    def predict(self, y_prob):
        return self.transform(y_prob)
