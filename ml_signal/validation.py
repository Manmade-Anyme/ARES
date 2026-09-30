import pandas as pd
import numpy as np
from typing import Iterator, Tuple, Dict, Any, List
import scipy.stats
from copy import deepcopy


def _class_counts(labels: pd.Series) -> Dict[str, int]:
    """Return a stable binary-label summary suitable for JSON reports."""
    numeric = pd.to_numeric(labels, errors="coerce")
    return {
        "negative": int((numeric == 0).sum()),
        "positive": int((numeric == 1).sum()),
    }


def _iso_timestamp(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    return value


def _finite_float(value: Any) -> Any:
    if isinstance(value, (int, float, np.number)) and np.isfinite(value):
        return float(value)
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return _iso_timestamp(value)
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def build_fold_result(
    fold_info: Dict[str, Any],
    train_labels: pd.Series,
    test_labels: pd.Series,
    *,
    auc: float = None,
    brier: float = None,
    degenerate: bool = False,
    reason: str = None,
) -> Dict[str, Any]:
    """Build the common, JSON-safe audit record emitted for every CV fold."""
    train_counts = _class_counts(train_labels)
    test_counts = _class_counts(test_labels)
    result = {
        "fold": int(fold_info["fold"]),
        "train_start": _iso_timestamp(fold_info.get("train_start")),
        "train_end": _iso_timestamp(fold_info.get("train_end")),
        "test_start": _iso_timestamp(fold_info.get("test_start")),
        "test_end": _iso_timestamp(fold_info.get("test_end")),
        "n_train": int(len(train_labels)),
        "n_test": int(len(test_labels)),
        "train_class_counts": train_counts,
        "test_class_counts": test_counts,
        "train_positive_rate": float(train_counts["positive"] / len(train_labels)) if len(train_labels) else None,
        "test_positive_rate": float(test_counts["positive"] / len(test_labels)) if len(test_labels) else None,
        "auc": _finite_float(auc),
        "brier": _finite_float(brier),
        "degenerate": bool(degenerate),
    }
    if reason is not None:
        result["reason"] = str(reason)
    return result

class WalkForwardPurgedCV:
    def __init__(
        self,
        n_splits: int = 5,
        min_train_samples: int = 100,
        embargo_window: pd.Timedelta = pd.Timedelta(minutes=15),
    ):
        self.n_splits = n_splits
        self.min_train_samples = min_train_samples
        self.embargo_window = embargo_window

    def split(
        self,
        df: pd.DataFrame,
        timestamp_col: str = "timestamp",
        resolution_col: str = "resolution_timestamp",
    ) -> Iterator[Tuple[np.ndarray, np.ndarray, Dict[str, Any]]]:
        n_samples = len(df)
        if n_samples < self.n_splits * 2:
            return

        # Simple expanding window based on integer divisions for demonstration
        # A real implementation might use time-based splitting or equal size
        indices = np.arange(n_samples)
        fold_size = n_samples // (self.n_splits + 1)

        for k in range(1, self.n_splits + 1):
            test_start_idx = k * fold_size
            test_end_idx = (k + 1) * fold_size if k < self.n_splits else n_samples
            
            test_idx_raw = indices[test_start_idx:test_end_idx]
            if len(test_idx_raw) == 0:
                continue

            test_start_timestamp = df.iloc[test_idx_raw[0]][timestamp_col]
            test_end_timestamp = df.iloc[test_idx_raw[-1]][timestamp_col]

            # Candidate train is all past data
            candidate_train_idx = indices[:test_start_idx]
            
            # Purge: resolution_timestamp >= test_start_timestamp
            candidate_train_res = df.iloc[candidate_train_idx][resolution_col]
            purge_mask = candidate_train_res >= test_start_timestamp
            
            # Embargo: timestamp >= test_start_timestamp - embargo_window
            candidate_train_ts = df.iloc[candidate_train_idx][timestamp_col]
            embargo_start = test_start_timestamp - self.embargo_window
            embargo_mask = candidate_train_ts >= embargo_start
            
            valid_train_mask = ~(purge_mask | embargo_mask)
            train_idx = candidate_train_idx[valid_train_mask]

            if len(train_idx) < self.min_train_samples:
                continue

            fold_info = {
                "fold": k,
                "train_start": df.iloc[train_idx[0]][timestamp_col],
                "train_end": df.iloc[train_idx[-1]][timestamp_col],
                "test_start": test_start_timestamp,
                "test_end": test_end_timestamp,
            }
            yield train_idx.values if isinstance(train_idx, pd.Series) else train_idx, test_idx_raw, fold_info


def compute_cv_metrics(fold_results: List[Dict[str, Any]], leakage_guard_passed: bool = True) -> Dict[str, Any]:
    evaluable_folds = 0
    degenerate_folds = 0
    aucs = []
    
    def is_finite_metric(value: Any) -> bool:
        return isinstance(value, (int, float, np.number)) and np.isfinite(value)

    def is_evaluable(res: Dict[str, Any]) -> bool:
        return (
            not res.get("degenerate", False)
            and is_finite_metric(res.get("auc"))
            and is_finite_metric(res.get("brier"))
            and int(res.get("n_test", 0)) > 0
        )

    for res in fold_results:
        if not is_evaluable(res):
            degenerate_folds += 1
        else:
            evaluable_folds += 1
            aucs.append(float(res["auc"]))
            
    metrics = {
        "validation_method": "walk_forward_purged",
        "evaluable_folds": evaluable_folds,
        "degenerate_folds": degenerate_folds,
        "leakage_guard_passed": leakage_guard_passed,
        "fold_results": _json_safe(deepcopy(fold_results)),
    }
    
    if evaluable_folds > 0:
        mean_auc = np.mean(aucs)
        metrics["mean_auc"] = mean_auc
        metrics["min_fold_auc"] = np.min(aucs)
        valid_res = [r for r in fold_results if is_evaluable(r)]
        total_n = sum(int(r["n_test"]) for r in valid_res)
        metrics["brier_score"] = (
            sum(float(r["brier"]) * int(r["n_test"]) for r in valid_res) / total_n
        )
        
        if evaluable_folds > 1:
            se = np.std(aucs, ddof=1) / np.sqrt(evaluable_folds)
            t_crit = float(scipy.stats.t.ppf(0.975, df=evaluable_folds - 1))
            metrics["ci_95_lower"] = mean_auc - t_crit * se
            metrics["ci_95_upper"] = mean_auc + t_crit * se
        else:
            metrics["ci_95_lower"] = mean_auc
            metrics["ci_95_upper"] = mean_auc
    else:
        metrics["mean_auc"] = np.nan
        metrics["min_fold_auc"] = np.nan
        metrics["brier_score"] = np.nan
        metrics["ci_95_lower"] = np.nan
        metrics["ci_95_upper"] = np.nan

    return metrics
