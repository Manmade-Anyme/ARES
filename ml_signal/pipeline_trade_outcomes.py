import pandas as pd
import numpy as np
from typing import List, Dict, Any, Tuple, Optional
import xgboost as xgb

from ml_signal.config import MLConfig, DEFAULT_CONFIG
from ml_signal.dataset import build_real_outcome_frame, feature_columns
from ml_signal.validation import WalkForwardPurgedCV, compute_cv_metrics
from ml_signal.leakage_guards import (
    assert_no_outcome_leakage,
    assert_chronological_integrity,
    assert_train_test_purged,
    deduplicate_snapshots
)
from ml_signal.pipeline_market_movement import MarketMovementPipeline

class TradeOutcomePipeline:
    def __init__(self, config: MLConfig = DEFAULT_CONFIG, use_hybrid_transfer: bool = True):
        self.config = config
        self.use_hybrid_transfer = use_hybrid_transfer
        
    def prepare_dataset(self, rows: List[dict], exit_timestamps: Dict[str, str] = None) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
            
        df = build_real_outcome_frame(rows)
        if df.empty:
            return df
            
        # Timestamp Anomaly Filtering
        df = df[df["time_metrics_excluded"] != True]
        df = df[df["exit_timestamp"].notna()]
        df["exit_timestamp"] = pd.to_datetime(df["exit_timestamp"])
        df["entry_timestamp"] = pd.to_datetime(df["entry_timestamp"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        
        # Valid trade duration
        df = df[df["exit_timestamp"] > df["entry_timestamp"]]
        df = df[df["exit_timestamp"] > df["timestamp"]]
        
        # Map resolution_timestamp
        df["resolution_timestamp"] = df["exit_timestamp"]
        
        df = deduplicate_snapshots(df)
        df = df.sort_values("timestamp")
        assert_chronological_integrity(df)
        
        feat_cols = feature_columns(df)
        assert_no_outcome_leakage(feat_cols)
        
        return df

    def run_walk_forward(
        self,
        df: pd.DataFrame,
        market_snapshots_df: Optional[pd.DataFrame] = None,
        n_splits: int = 5
    ) -> Tuple[object, Dict[str, Any]]:
        if df.empty:
            return None, {}
            
        cv = WalkForwardPurgedCV(
            n_splits=n_splits,
            embargo_window=pd.Timedelta(minutes=30) # config.trade_outcome_embargo_minutes
        )
        
        fold_results = []
        oof_preds = np.zeros(len(df))
        oof_labels = np.zeros(len(df))
        
        stage1_pipeline = MarketMovementPipeline(self.config)
        stage1_feat_cols = []
        if market_snapshots_df is not None and not market_snapshots_df.empty:
            stage1_feat_cols = [c for c in feature_columns(market_snapshots_df) if not c.startswith("detector_scores__")]
            
        for train_idx, test_idx, fold_info in cv.split(df, timestamp_col="timestamp", resolution_col="resolution_timestamp"):
            train_df = df.iloc[train_idx].copy()
            test_df = df.iloc[test_idx].copy()
            
            assert_train_test_purged(train_df, test_df)
            
            if len(test_df["label"].unique()) < 2 or len(train_df["label"].unique()) < 2:
                fold_results.append({"fold": fold_info["fold"], "degenerate": True})
                continue

            # Stage 1 cross-fitting
            if self.use_hybrid_transfer and market_snapshots_df is not None:
                # Outer test rows scoring:
                test_start = fold_info["test_start"]
                # snapshots strictly resolved before test_start
                valid_snaps = market_snapshots_df[market_snapshots_df["resolution_timestamp"] < test_start]
                if len(valid_snaps) > 0 and valid_snaps["label"].nunique() > 1:
                    model_s1 = xgb.XGBClassifier(n_estimators=50, max_depth=3, random_state=42)
                    model_s1.fit(valid_snaps[stage1_feat_cols], valid_snaps["label"])
                    test_df["meta_features__market_movement_prob"] = model_s1.predict_proba(test_df.reindex(columns=stage1_feat_cols))[:, 1]
                else:
                    test_df["meta_features__market_movement_prob"] = 0.5
                    
                # Outer train rows scoring (inner OOF):
                train_probs = []
                for _, row in train_df.iterrows():
                    entry_ts = row["timestamp"]
                    inner_snaps = market_snapshots_df[market_snapshots_df["resolution_timestamp"] < entry_ts]
                    if len(inner_snaps) > 0 and inner_snaps["label"].nunique() > 1:
                        model_inner = xgb.XGBClassifier(n_estimators=50, max_depth=3, random_state=42)
                        model_inner.fit(inner_snaps[stage1_feat_cols], inner_snaps["label"])
                        prob = model_inner.predict_proba(pd.DataFrame([pd.DataFrame([row]).reindex(columns=stage1_feat_cols).iloc[0]]))[:, 1][0]
                    else:
                        prob = 0.5
                    train_probs.append(prob)
                train_df["meta_features__market_movement_prob"] = train_probs

            # Stage 2 training
            feat_cols2 = ["meta_features__market_movement_prob"] if self.use_hybrid_transfer else []
            # Add canonical structural features
            feat_cols2 += [
                c for c in [
                    "structure_features__dist_to_nearest_support",
                    "structure_features__dist_to_nearest_resistance",
                    "oi_features__pcr_oi",
                    "candle_features__body_pct",
                    "iv_features__iv_level",
                    "greek_features__net_delta"
                ] if c in df.columns
            ]
            
            X_train = train_df[feat_cols2]
            y_train = train_df["label"]
            X_test = test_df[feat_cols2]
            y_test = test_df["label"]
            
            # Constrained hyperparameters
            model = xgb.XGBClassifier(
                n_estimators=50,
                learning_rate=0.03,
                max_depth=2,
                reg_lambda=5.0,
                reg_alpha=1.0,
                colsample_bytree=0.6,
                subsample=0.7,
                random_state=42,
                early_stopping_rounds=None
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
        if len(df) < 500:
            metrics["provisional_sample_size"] = True
            
        # We don't return final model here as that's handled in the orchestrator
        # or we return a dummy for now.
        return None, metrics
