import pandas as pd
import numpy as np
from typing import Iterator, Tuple, Dict, Any, List
import scipy.stats

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
    
    for res in fold_results:
        # Assuming res contains 'auc', 'degenerate'
        if res.get("degenerate", False) or np.isnan(res.get("auc", np.nan)):
            degenerate_folds += 1
        else:
            evaluable_folds += 1
            aucs.append(res["auc"])
            
    metrics = {
        "validation_method": "walk_forward_purged",
        "evaluable_folds": evaluable_folds,
        "degenerate_folds": degenerate_folds,
        "leakage_guard_passed": leakage_guard_passed,
    }
    
    if evaluable_folds > 0:
        mean_auc = np.mean(aucs)
        metrics["mean_auc"] = mean_auc
        metrics["min_fold_auc"] = np.min(aucs)
        valid_res = [r for r in fold_results if not r.get("degenerate", False)]
        if valid_res and "n_test" in valid_res[0]:
            total_n = sum(r["n_test"] for r in valid_res)
            metrics["brier_score"] = sum(r["brier"] * r["n_test"] for r in valid_res) / total_n if total_n > 0 else np.nan
        else:
            metrics["brier_score"] = np.mean([r["brier"] for r in valid_res])
        
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
