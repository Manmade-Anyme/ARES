from typing import Dict, Any, Tuple, List

class ModelPromotionError(Exception):
    def __init__(self, message, reasons=None):
        super().__init__(message)
        self.reasons = reasons or []

def evaluate_promotion_gate(metrics: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons = []
    
    if metrics.get("validation_method") != "walk_forward_purged":
        reasons.append("Validation method must be walk_forward_purged.")
        
    evaluable = metrics.get("evaluable_folds", 0)
    if evaluable < 4:
        reasons.append(f"Requires at least 4 evaluable folds, got {evaluable}.")
        
    degenerate = metrics.get("degenerate_folds", 0)
    if degenerate > 0:
        reasons.append(f"Zero degenerate folds allowed, got {degenerate}.")
        
    mean_auc = metrics.get("mean_auc", 0.0)
    if mean_auc < 0.55:
        reasons.append(f"Mean AUC must be >= 0.55, got {mean_auc}.")
        
    ci_95_lower = metrics.get("ci_95_lower", 0.0)
    if ci_95_lower <= 0.50:
        reasons.append(f"Lower 95% CI must be > 0.50, got {ci_95_lower}.")
        
    min_fold_auc = metrics.get("min_fold_auc", 0.0)
    if min_fold_auc < 0.40:
        reasons.append(f"Min fold AUC must be >= 0.40, got {min_fold_auc}.")
        
    brier = metrics.get("brier_score", 1.0)
    if brier > 0.23:
        reasons.append(f"Brier score must be <= 0.23, got {brier}.")
        
    if not metrics.get("leakage_guard_passed", False):
        reasons.append("Leakage guard failed.")
        
    return len(reasons) == 0, reasons

def enforce_promotion_or_raise(metrics: Dict[str, Any]) -> None:
    passed, reasons = evaluate_promotion_gate(metrics)
    if not passed:
        raise ModelPromotionError(f"Promotion gate failed: {reasons}", reasons=reasons)
