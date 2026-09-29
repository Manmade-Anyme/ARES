"""Fail-safe confidence gate backed by out-of-sample tier validation."""
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ConfidenceCalibrationPolicy:
    """Keep HIGH confidence disabled until both OOS superiority tests pass."""

    def __init__(self, validation_record_path: Optional[str] = None, alpha: float = 0.05,
                 min_samples: int = 30):
        if not 0.0 < alpha < 1.0:
            raise ValueError("alpha must be in (0, 1)")
        if min_samples < 1:
            raise ValueError("min_samples must be positive")
        self.alpha = alpha
        self.min_samples = min_samples
        if validation_record_path is None:
            validation_record_path = str(
                Path(__file__).resolve().parent.parent
                / "reports" / "ml" / "calibration_validation_record.json"
            )
        record_path = Path(validation_record_path)
        if not record_path.is_absolute():
            record_path = Path(__file__).resolve().parent.parent / record_path
        self.validation_record_path = str(record_path)
        self.validation_record = self._load_record(self.validation_record_path)
        self.stratified_record = self._load_record(
            record_path.parent / "confidence_calibration_stratified_report.json"
        )

    @staticmethod
    def _load_record(path):
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
            return record if isinstance(record, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _significance(self, for_ml: bool = False):
        record = self.validation_record or {}
        if for_ml and isinstance(record.get("ml_tier_significance"), dict):
            result = dict(record["ml_tier_significance"])
            for key in ("auc_roc", "probability_calibrated", "model_version"):
                if key in record:
                    result[key] = record[key]
            return result
        if for_ml:
            return {}
        result = record.get("overall", record)
        if isinstance(result, dict) and isinstance(result.get("significance"), dict):
            result = result["significance"]
        elif isinstance(record.get("tier_significance"), dict):
            result = record["tier_significance"]
        return result if isinstance(result, dict) else {}

    def _result_is_superior(self, result: Dict[str, Any]) -> bool:
        high_n = result.get("high_count", result.get("high_samples", 0))
        medium_n = result.get("medium_count", result.get("medium_samples", 0))
        try:
            return (
                int(high_n) >= self.min_samples
                and int(medium_n) >= self.min_samples
                and result.get("is_statistically_superior") is True
                and float(result.get("fisher_p_value", 1.0)) < self.alpha
                and float(result.get("mann_whitney_p_value", 1.0)) < self.alpha
                and float(result.get("win_rate_diff", 0.0)) > 0.0
                and float(result.get("expectancy_diff", 0.0)) > 0.0
            )
        except (TypeError, ValueError, OverflowError):
            return False

    def _detector_strata(self, setup_type: str, direction: str):
        report = self.stratified_record or {}
        setup = getattr(setup_type, "value", setup_type)
        directional = getattr(direction, "value", direction)
        def significance_for(axis, value):
            records = report.get(axis)
            row = records.get(str(value)) if isinstance(records, dict) else None
            result = row.get("significance") if isinstance(row, dict) else None
            return result if isinstance(result, dict) else {}

        setup_result = significance_for("by_setup", setup)
        direction_result = significance_for("by_direction", directional)
        return [setup_result, direction_result]

    def check_validation_status(self, setup_type: str, direction: str) -> bool:
        """Report whether this detector setup and direction may emit HIGH."""
        return self.is_high_validated(setup_type=setup_type, direction=direction)

    def is_calibrator_validated(self, model_version: Optional[str]) -> bool:
        """Require held-out approval for this exact model before using its sidecar."""
        record = self.validation_record or {}
        return bool(
            model_version
            and record.get("model_version") == model_version
            and record.get("probability_calibrated") is True
        )

    @staticmethod
    def _valid_auc(result: Dict[str, Any]) -> Optional[float]:
        value = result.get("auc_roc")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            auc = float(value)
        except OverflowError:
            return None
        return auc if math.isfinite(auc) and 0.0 <= auc <= 1.0 else None

    def is_high_validated(
        self,
        for_ml: bool = False,
        model_version: Optional[str] = None,
        setup_type: Optional[str] = None,
        direction: Optional[str] = None,
        runtime_calibrated: bool = False,
    ) -> bool:
        result = self._significance(for_ml=for_ml)
        if not self._result_is_superior(result):
            return False
        if for_ml:
            auc = self._valid_auc(result)
            return bool(
                auc is not None and auc >= 0.5
                and self.is_calibrator_validated(model_version)
                and runtime_calibrated
            )
        strata = self._detector_strata(setup_type, direction)
        return len(strata) == 2 and all(self._result_is_superior(item) for item in strata)

    def evaluate_signal_tier(
        self,
        setup_type: str,
        direction: str,
        tentative_tier: str,
        heuristic_score: int,
        max_score: int,
        reasons: List[str],
        market_context: Dict[str, Any],
    ) -> Tuple[str, List[str]]:
        """Downgrade unvalidated HIGH and preserve a specific audit reason."""
        is_ml = str(setup_type).upper() == "ML_PREDICTION"
        if str(tentative_tier).upper() != "HIGH" or self.is_high_validated(
            for_ml=is_ml,
            model_version=market_context.get("model_version") if is_ml else None,
            setup_type=setup_type,
            direction=direction,
            runtime_calibrated=market_context.get("calibrator_applied") is True if is_ml else False,
        ):
            return tentative_tier, reasons
        result = self._significance(for_ml=is_ml) if is_ml else next(
            (item for item in self._detector_strata(setup_type, direction)
             if not self._result_is_superior(item)),
            {},
        )
        p_value = result.get("fisher_p_value", 1.0)
        win_diff = result.get("win_rate_diff", 0.0)
        try:
            p_text = f"{float(p_value):.4f}"
            delta_text = f"{float(win_diff):+.1%}"
        except (TypeError, ValueError):
            p_text, delta_text = "1.0000", "+0.0%"
        reasons = list(reasons)
        auc = self._valid_auc(result) if is_ml else None
        if is_ml and auc is not None and auc < 0.5:
            detail = f"inverted AUC ({auc:.4f} < 0.5000)"
        elif is_ml and not self.is_calibrator_validated(market_context.get("model_version")):
            detail = "calibrator has no held-out approval for this model"
        elif is_ml and market_context.get("calibrator_applied") is not True:
            detail = "validated runtime calibrator unavailable"
        elif is_ml and auc is None and self._result_is_superior(result):
            detail = "valid AUC evidence unavailable"
        else:
            detail = (
                "out-of-sample validation not statistically superior "
                f"(p={p_text}, Δwin={delta_text})"
            )
        reasons.append(f"[CONFIDENCE GATE] HIGH tier suppressed to MEDIUM: {detail}")
        market_context["uncalibrated_high_suppressed"] = True
        return "MEDIUM", reasons

    def apply_to_signal(self, signal):
        """Apply the gate to a completed signal, preserving its existing context."""
        context = signal.market_context if signal.market_context is not None else {}
        context.setdefault("tentative_confidence", signal.confidence)
        signal.confidence, signal.reasons = self.evaluate_signal_tier(
            setup_type=getattr(signal.setup_type, "value", signal.setup_type),
            direction=getattr(signal.direction, "value", signal.direction),
            tentative_tier=signal.confidence,
            heuristic_score=0,
            max_score=0,
            reasons=signal.reasons,
            market_context=context,
        )
        signal.market_context = context
        return signal


# Detector modules import this once at process startup; live signals reuse the
# loaded validation data without reading files in the candle generation path.
DEFAULT_DETECTOR_CALIBRATION_POLICY = ConfidenceCalibrationPolicy()
