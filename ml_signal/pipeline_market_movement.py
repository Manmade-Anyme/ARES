import pandas as pd
import numpy as np
from typing import List, Dict, Any, Tuple, Optional
import xgboost as xgb
from copy import deepcopy

from ml_signal.config import MLConfig, DEFAULT_CONFIG
from ml_signal.dataset import build_labeled_frame, feature_columns
from ml_signal.validation import WalkForwardPurgedCV, compute_cv_metrics
from ml_signal.leakage_guards import (
    assert_no_outcome_leakage,
    assert_chronological_integrity,
    assert_train_test_purged,
    deduplicate_snapshots
)

class MarketMovementPipeline:
    def __init__(self, config: MLConfig = DEFAULT_CONFIG):
        self.config = config
        
    def prepare_dataset(self, rows: List[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
            
        df = build_labeled_frame(
            rows, 
            lookforward=self.config.lookforward_candles,
            tp_points=self.config.mm_tp_points, # or market_movement_tp_points if added to config
            sl_points=self.config.mm_sl_points  # or market_movement_sl_points if added to config
        )
        if df.empty:
            return df
            
        assert_chronological_integrity(df)
        
        # Strictly exclude detector_scores
        feat_cols = feature_columns(df)
        feat_cols = [c for c in feat_cols if not c.startswith("detector_scores__")]
        assert_no_outcome_leakage(feat_cols)
        
        return df

    def run_walk_forward(self, df: pd.DataFrame, n_splits: int = 5) -> Tuple[object, Dict[str, Any]]:
        if df.empty:
            return None, {}
            
        feat_cols = [c for c in feature_columns(df) if not c.startswith("detector_scores__")]
        
        cv = WalkForwardPurgedCV(
            n_splits=n_splits,
            embargo_window=pd.Timedelta(minutes=15) # config.market_movement_embargo_minutes
        )
        
        fold_results = []
        oof_preds = np.zeros(len(df))
        oof_labels = np.zeros(len(df))
        
        for train_idx, test_idx, fold_info in cv.split(df, timestamp_col="timestamp", resolution_col="resolution_timestamp"):
            train_df = df.iloc[train_idx]
            test_df = df.iloc[test_idx]
            
            assert_train_test_purged(train_df, test_df)
            
            X_train = train_df[feat_cols]
            y_train = train_df["label"]
            X_test = test_df[feat_cols]
            y_test = test_df["label"]
            
            if len(y_test.unique()) < 2 or len(y_train.unique()) < 2:
                fold_results.append({"fold": fold_info["fold"], "degenerate": True})
                continue
                
            model = xgb.XGBClassifier(
                n_estimators=self.config.n_estimators,
                learning_rate=self.config.learning_rate,
                max_depth=self.config.max_depth,
                subsample=self.config.subsample,
                colsample_bytree=self.config.colsample_bytree,
                random_state=42,
                early_stopping_rounds=None # No outer test leaks
            )
            
            model.fit(X_train, y_train, eval_set=None)
            preds = model.predict_proba(X_test)[:, 1]
            
            oof_preds[test_idx] = preds
            oof_labels[test_idx] = y_test
            
            from sklearn.metrics import roc_auc_score, brier_score_loss
            auc = roc_auc_score(y_test, preds)
            brier = brier_score_loss(y_test, preds)
            
            fold_results.append({
                "fold": fold_info["fold"],
                "auc": auc,
                "brier": brier,
                "degenerate": False
            })
            
        metrics = compute_cv_metrics(fold_results, leakage_guard_passed=True)
        
        # Fit final model on all data
        final_model = xgb.XGBClassifier(
            n_estimators=self.config.n_estimators,
            learning_rate=self.config.learning_rate,
            max_depth=self.config.max_depth,
            subsample=self.config.subsample,
            colsample_bytree=self.config.colsample_bytree,
            random_state=42
        )
        final_model.fit(df[feat_cols], df["label"])
        
        return final_model, metrics
