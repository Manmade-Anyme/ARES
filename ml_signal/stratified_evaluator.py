"""Calibration and tier-performance reporting across operational market strata."""
from dataclasses import asdict, dataclass
from datetime import time
import json
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from .calibration import compute_brier_decomposition, compute_calibration_curve, test_tier_significance


@dataclass
class StratifiedCalibrationReport:
    by_setup: Dict[str, Dict[str, Any]]
    by_direction: Dict[str, Dict[str, Any]]
    by_volatility_regime: Dict[str, Dict[str, Any]]
    by_time_of_day: Dict[str, Dict[str, Any]]
    overall: Dict[str, Any]
    overall_brier: Optional[Dict[str, Any]] = None
    overall_ece: Optional[float] = None

    def to_dict(self):
        return asdict(self)


class StratifiedCalibrationEvaluator:
    def __init__(self, df: pd.DataFrame):
        self.df = df.copy()

    @staticmethod
    def _first_column(frame, names):
        return next((name for name in names if name in frame.columns), None)

    def _prepare(self):
        frame = self.df.copy()
        aliases = {
            "confidence": ["confidence", "tier", "signal_tentative_confidence", "signal_confidence", "confidence_tier"],
            "win": ["win", "label", "outcome"],
            "pnl": ["pnl_points", "trade_pnl", "pnl"],
            "setup": ["setup_type", "signal_setup_type", "setup"],
            "direction": ["direction", "signal_direction"],
            "iv": ["iv_level", "iv_features__iv_level", "implied_volatility", "iv", "india_vix"],
            "timestamp": ["timestamp", "entry_timestamp", "signal_timestamp"],
            "probability": ["probability", "y_prob", "predicted_probability"],
        }
        cols = {key: self._first_column(frame, values) for key, values in aliases.items()}
        for key in ("confidence", "win", "pnl"):
            if cols[key] is None:
                raise ValueError(f"missing required {key} column")
        for key, col in cols.items():
            if col and col != key:
                frame[key] = frame[col]
        for key in ("setup", "direction"):
            if key in frame:
                frame[key] = frame[key].map(lambda value: getattr(value, "value", value))
        frame["confidence"] = frame["confidence"].astype(str).str.upper()
        frame["win"] = pd.to_numeric(frame["win"], errors="coerce")
        frame["pnl"] = pd.to_numeric(frame["pnl"], errors="coerce")
        return frame, cols

    @staticmethod
    def _time_bucket(value):
        if pd.isna(value):
            return "UNKNOWN"
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Kolkata")
        else:
            ts = ts.tz_convert("Asia/Kolkata")
        clock = ts.time()
        if time(9, 15) <= clock < time(10, 30):
            return "09:15-10:30"
        if time(10, 30) <= clock < time(13, 30):
            return "10:30-13:30"
        if time(13, 30) <= clock <= time(15, 30):
            return "13:30-15:30"
        return "OUTSIDE_SESSION"

    @staticmethod
    def _stratum_summary(frame, alpha):
        frame = frame.dropna(subset=["confidence", "win", "pnl"])
        by_tier = {}
        for tier in ("HIGH", "MEDIUM"):
            part = frame[frame["confidence"] == tier]
            by_tier[tier] = {
                "sample_size": int(len(part)),
                "win_rate": float(part["win"].mean()) if len(part) else None,
                "mean_expectancy": float(part["pnl"].mean()) if len(part) else None,
            }
        result = {
            "sample_size": int(len(frame)),
            "by_tier": by_tier,
            "significance": test_tier_significance(
                frame,
                tier_col="confidence", outcome_col="win", pnl_col="pnl", alpha=alpha,
            ).to_dict(),
            "brier": None,
            "calibration": None,
        }
        if "probability" in frame:
            valid = frame.dropna(subset=["probability"])
            if len(valid):
                y, p = valid["win"].to_numpy(), valid["probability"].to_numpy()
                try:
                    result["brier"] = asdict(compute_brier_decomposition(y, p))
                    curve = compute_calibration_curve(y, p)
                    result["calibration"] = asdict(curve)
                except ValueError:
                    pass
        return result

    def evaluate(self, alpha: float = 0.05) -> StratifiedCalibrationReport:
        frame, cols = self._prepare()
        valid = frame[frame["confidence"].isin(["HIGH", "MEDIUM"])].copy()

        def evaluate_axis(column):
            if not column or column not in valid:
                return {}
            return {
                str(value): self._stratum_summary(part, alpha)
                for value, part in valid.groupby(column, dropna=False, sort=True)
            }

        by_setup = evaluate_axis("setup")
        by_direction = evaluate_axis("direction")

        volatility = {}
        if cols["iv"]:
            iv = pd.to_numeric(valid["iv"], errors="coerce")
            median = float(iv.median()) if iv.notna().any() else np.nan
            if np.isfinite(median):
                valid["volatility_regime"] = np.where(
                    iv.isna(), "UNKNOWN", np.where(iv <= median, "LOW_IV", "HIGH_IV")
                )
            else:
                valid["volatility_regime"] = "UNKNOWN"
            volatility = evaluate_axis("volatility_regime")

        by_time = {}
        if cols["timestamp"]:
            valid["time_of_day"] = valid["timestamp"].map(self._time_bucket)
            by_time = evaluate_axis("time_of_day")

        overall = self._stratum_summary(valid, alpha)
        return StratifiedCalibrationReport(
            by_setup=by_setup,
            by_direction=by_direction,
            by_volatility_regime=volatility,
            by_time_of_day=by_time,
            overall=overall,
            overall_brier=overall["brier"],
            overall_ece=(overall["calibration"]["ece"] if overall["calibration"] else None),
        )

    def export_json(self, path: str = "reports/ml/confidence_calibration_stratified_report.json",
                    alpha: float = 0.05) -> None:
        report = self.evaluate(alpha=alpha)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.to_dict(), indent=2, allow_nan=False), encoding="utf-8")
