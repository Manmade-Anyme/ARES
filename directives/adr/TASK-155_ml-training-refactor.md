# ADR-155: Decoupled ML Pipelines, Purged Walk-Forward Cross-Validation, Data Leakage Guards & Production Promotion Gating

- **Status**: Proposed
- **Date**: 2026-09-12
- **Task ID**: TASK-155 / MANM-155
- **Author**: Software Architect Agent (`d2d4e328-096d-4658-8d90-44aa7b51ed05`)
- **Target Components**: `ml_signal/pipeline_market_movement.py`, `ml_signal/pipeline_trade_outcomes.py`, `ml_signal/validation.py`, `ml_signal/leakage_guards.py`, `ml_signal/promotion_gate.py`, `ml_signal/train_offline.py`, `ml_signal/dataset.py`, `ml_signal/config.py`, `ml_signal/predictor.py`

---

## 1. Executive Summary & Root Cause Analysis

### 1.1 Problem Statement & Background
The ARES machine learning pipeline currently conflates two structurally distinct predictive tasks into a single entrypoint script (`ml_signal/train_offline.py`):
1. **Continuous Market-Movement / Forward-Price Dynamics**: Labeled via forward price trajectories (triple-barrier method) across all 14,823 market snapshots, yielding 617 surviving clean directional moves (199 positive, 418 negative; 32.3% positive rate).
2. **Discrete Realized Trade Outcomes**: Labeled via real trade execution results (`trade_analytics` / `active_trades`), currently containing only 232 usable closed trades (~32.3% win rate).

Historically, in TASK-183, `train_offline.py` trained an XGBoost model on self-labeled forward price snapshots. In TASK-197, `train_offline.py` was directly altered to call `build_real_outcome_frame`, overriding the forward-price pipeline and creating a brittle, single-path architecture.

### 1.2 Flaws in Current Validation & Evidence
1. **Single Chronological Holdout Split Flaw**:
   - `chronological_split(df, train_frac=0.8)` splits data once by timestamp.
   - For realized trades ($N = 232$), this assigns 185 trades to training and 47 trades to testing.
   - In `reports/ml/v8_offline_metrics.json`, the single test fold yielded an **AUC of 0.418** (worse than random guessing) with recall of 17.6%.
   - In-memory retrain experiments on forward-price snapshots claimed an AUC of 0.674, but that too was evaluated on a single chronological tail.
   - **Risk**: Financial time series are highly non-stationary. A single holdout window reflects the idiosyncratic market regime of that specific calendar period (e.g., low-volatility summer grind vs. high-volatility event days), creating severe regime overfitting, misleading metrics, and random model promotion.
2. **Sample Size Starvation ($N = 232$)**:
   - Training 40–60 feature trees on 185 training rows severely violates Harrell’s heuristic and the Vapnik-Chervonenkis bounds (requiring $\ge 10\text{--}20$ events per parameter). With only ~60 positive trade events in training, high-capacity gradient boosted trees overfit the training split and collapse on out-of-sample data.
3. **Data Leakage Vulnerabilities**:
   - **Outcome Field Leakage**: Absence of a strict programmatic barrier between database bookkeeping/outcome columns (`trade_outcome`, `trade_pnl`, `exit_price`, `exit_timestamp`) and feature matrices.
   - **Target / Window Overlap Leakage**: Triple-barrier forward labels span 5 lookforward candles (15–25 minutes); trades span variable holding periods (minutes to hours). Splitting at a single cutoff timestamp without a **purge buffer** causes training labels to incorporate price action extending into the test evaluation period.
   - **Snapshot Duplication**: Multiple snapshot rows captured within the same minute for identical signals artificially inflate sample counts and leak identical feature vectors across split boundaries.
4. **Absence of a Production Gate**:
   - Models are automatically incremented and saved to `ml_signal/models/v{N}.joblib` regardless of whether validation demonstrated statistically significant out-of-fold alpha.

---

## 2. Decoupled Architecture & System Design

```
+---------------------------------------------------------------------------------------------------+
|                                      `ml_collection` (14,823 snapshots)                           |
+-------------------------------------------------+-------------------------------------------------+
                                                  |
                         +------------------------+------------------------+
                         |                                                 |
                         v                                                 v
    +-----------------------------------------+   +-------------------------------------------------+
    |   Pipeline 1: Market-Movement Model     |   |       Pipeline 2: Realized-Trade Outcome Model  |
    |   (`ml_signal/pipeline_market_movement`) |   |       (`ml_signal/pipeline_trade_outcomes`)     |
    +-----------------------------------------+   +-------------------------------------------------+
    | - Source: All raw cycle snapshots       |   | - Source: Trade-linked rows (N=232)             |
    | - Target: Forward price path (triple    |   | - Target: Realized execution outcome (T1/T2 hit)|
    |   barrier: +/- 15 pts before stop)      |   | - Sample Starvation Mitigation:                 |
    | - Filter: Drops inconclusive (-1)       |   |   Hybrid Transfer / Feature Stacking:           |
    | - Dataset: N ~ 617 high-conviction      |   |   Consumes Stage 1 market probability prior     |
    |   movements                             |   |   + regularized shallow trees (max_depth=2)     |
    +--------------------+--------------------+   +------------------------+------------------------+
                         |                                                 |
                         +------------------------+------------------------+
                                                  |
                                                  v
                     +---------------------------------------------------------+
                     |         Shared Validation & Leakage Guard Engine        |
                     +---------------------------------------------------------+
                     | - `ml_signal/leakage_guards.py`:                        |
                     |   * Forbidden outcome column blacklist asserts          |
                     |   * Monotonic chronological ordering verification       |
                     |   * Lookforward / Trade duration window purging         |
                     |   * Snapshot deduplication                              |
                     | - `ml_signal/validation.py`:                            |
                     |   * Purged & Embargoed Walk-Forward Cross-Validation    |
                     |   * K-Fold Rolling/Expanding Splits (K >= 4)            |
                     |   * Out-of-fold (OOF) pooled prediction evaluation      |
                     |   * 95% Confidence Intervals (Student's t / SE)         |
                     +----------------------------+----------------------------+
                                                  |
                                                  v
                     +---------------------------------------------------------+
                     |       Production Promotion Gate (`promotion_gate.py`)   |
                     +---------------------------------------------------------+
                     | Criteria for `v{N}.joblib` active promotion:            |
                     | 1. Method: `walk_forward_purged` (K >= 4 folds)         |
                     | 2. Mean OOF AUC >= 0.55                                 |
                     | 3. Lower 95% Confidence Bound > 0.50                    |
                     | 4. Max fold degradation: Zero folds with AUC < 0.40     |
                     | 5. Brier Score <= 0.23                                  |
                     | 6. Zero data leakage violations                         |
                     +---------------------------------------------------------+
```

### 2.1 Component Decoupling & Interface Contracts
To achieve complete architectural decoupling without breaking external callers:

1. **Market Movement Pipeline (`ml_signal/pipeline_market_movement.py`)**:
   - Encapsulates dataset preparation via `build_labeled_frame`.
   - Focuses strictly on market microstructure dynamics: predicting whether price moves $\ge 15.0$ points before hitting $-10.0$ points stop within 5 candles ($N \approx 617$ resolved high-conviction moves out of 14,823 snapshots, 32.3% positive rate).
   - **Market-Only Feature Scope**: Stage 1 models market microstructure dynamics across all 14,823 continuous snapshots. Because continuous background cycle snapshots do not have fired detector setups, and `build_feature_vector()` generates market features at inference time without detector scores, `detector_scores` **MUST BE EXCLUDED** from Stage 1 training. Stage 1 is trained strictly on market feature groups: `candle_features`, `volume_features`, `iv_features`, `oi_features`, `greek_features`, `structure_features`, and `meta_features` (plus structural indicators). This guarantees `stage1_feature_names` is 100% covered by `build_feature_vector()` with zero missing or NaN detector columns at live serving time.
   - **Threshold Reconciliation**: Microstructure momentum thresholds (`market_movement_tp_points = 15.0`, `market_movement_sl_points = 10.0`) are explicitly added to `MLConfig` in `ml_signal/config.py` and passed directly to `build_labeled_frame()`. The system's trade execution targets (`tp_points = 35.0`, `sl_points = 25.0`) remain dedicated to realized trade evaluation in `TradeOutcomePipeline`.
   - **Artifacts**: Secondary diagnostic report `reports/ml/market_movement_metrics.json`, research artifact `ml_signal/models/market_movement_v{N}.joblib`.

2. **Realized Trade Outcome Pipeline (`ml_signal/pipeline_trade_outcomes.py`)**:
   - Encapsulates dataset preparation via `build_real_outcome_frame` on realized trades ($N = 232$).
   - Focuses strictly on execution reliability given signal entry conditions.
   - Evaluates whether execution sample size is sufficient ($N < 500$ triggers `provisional_sample_size: true`) and executes hybrid transfer learning.
   - **Artifacts**: Secondary diagnostic report `reports/ml/trade_outcome_metrics.json`.

3. **Active Production Artifact Contract (`ml_signal/models/v{N}.joblib`) & Target Disambiguation**:
   - `SignalPredictor.load_model()` in `ml_signal/predictor.py` discovers models via `discover_latest_model()` matching the strict regex `^v(\d+)\.joblib$`.
   - **Single Production Target Rule**: Production live alert evaluation requires trade success reliability, not raw 15-point market movement probability. Therefore, the **Trade Outcome pipeline** (or the `HybridPredictorBundle` produced by it) is the **sole pipeline permitted to occupy the canonical `ml_signal/models/v{N}.joblib` slot**.
   - When running `--pipeline all --promote`:
     - `MarketMovementPipeline` trains and writes its output strictly to `ml_signal/models/market_movement_v{N}.joblib`.
     - `TradeOutcomePipeline` trains the hybrid model (incorporating Stage 1 market representation), evaluates the promotion gate, and—upon gate passage—promotes the self-contained `HybridPredictorBundle` to `ml_signal/models/v{N}.joblib` (incrementing $N$).
     - This completely prevents race conditions or accidental overwrite of trade-success scoring with market-microstructure models.

4. **Workflow Metrics Output Contract & Schema Preservation (`reports/ml/task183_offline_metrics.json`)**:
   - The scheduled training workflow `.github/workflows/ml_training.yml:35-43` executes `python -m ml_signal.train_offline` and asserts `test -f reports/ml/task183_offline_metrics.json` (respecting `METRICS_PATH`).
   - `ml_signal/train_offline.py` **MUST** default to writing the primary summary metrics output to `os.environ.get("METRICS_PATH", "reports/ml/task183_offline_metrics.json")` in addition to individual pipeline diagnostic JSONs.
   - **Schema Compatibility with Workflow Consumer**:
     `.github/workflows/ml_training.yml:73-82` consumes top-level JSON fields for weekly Discord reporting and artifact publishing. The primary metrics JSON **MUST** provide legacy-compatible top-level keys alongside walk-forward validation fields:
     - `model_version`: string (e.g., `"v10"`).
     - `auc_roc`: float (aliasing `mean_auc` / pooled out-of-fold AUC).
     - `sharpe_status`: string (`"computed"` or `"insufficient_trades"`).
     - `sharpe_annualized`, `sharpe_daily`, `sharpe_days`, `sharpe_trades`, `sharpe_window_start`, `sharpe_window_end`, `sharpe_total_pnl_points`.
     - `shap_status`: string (`"computed"` or `"skipped"`), `shap_computed`: bool.
     - `promoted`: bool (`true` if gate passed and promoted, `false` otherwise).
     - `leakage_guard_passed`: bool.

5. **Unified CLI Orchestrator (`ml_signal/train_offline.py`) & Gated Promotion Default**:
   - Serves as the high-level entrypoint supporting CLI arguments:
     `python -m ml_signal.train_offline --pipeline [market_movement | trade_outcomes | all] --folds 5 --promote --metrics-path reports/ml/task183_offline_metrics.json`.
   - **Gated Promotion by Default**: `train_offline.py` defaults to `--promote` (which is **strictly gated** by `promotion_gate.py`). If the candidate passes all gate criteria, it promotes to canonical `v{N}.joblib`; if the gate fails, promotion is blocked/rejected (candidate saved as `_unpromoted.joblib` and canonical pointer remains untouched). `--no-promote` is available as an explicit opt-out flag for exploratory/research runs.
   - **Scheduled Workflow Alignment**: Defaulting to gated promotion ensures scheduled runs in `.github/workflows/ml_training.yml` can automatically promote candidates that pass all validation criteria, while unvetted candidates are safely rejected without manual intervention. The workflow will explicitly specify `--promote` for auditability.
   - `--pipeline all` executes Stage 1 followed by Stage 2 with automatic cross-fitting. Only Stage 2 is eligible for canonical promotion.

---

## 3. Purged & Embargoed Walk-Forward Cross-Validation Specification

### 3.1 Mathematical Formulation
Financial time series cannot be partitioned with randomized $K$-fold or simple chronological single splits. We define an **Expanding Window Walk-Forward Split with Purging and Embargoing**:

Given chronologically ordered dataset $\mathcal{D} = \{(x_i, y_i, t_i^{\text{start}}, t_i^{\text{end}})\}_{i=1}^N$ with $K$ validation folds ($K \ge 4$):
For fold $k \in \{1, \dots, K\}$:
1. **Test Window**: 
   The test period is bounded by timestamps $[T_{k}^{\text{test\_start}}, T_{k}^{\text{test\_end}}]$.
2. **Train Window**: 
   The candidate train period includes all historical observations where $t_i^{\text{start}} < T_{k}^{\text{test\_start}}$.
3. **Purging Barrier (Actual Resolution Timestamp)**:
   An observation $i$ in the candidate training set is **purged** if its actual forward label evaluation horizon or trade duration touches or overlaps the test start:
   $$\text{Purge Condition: } t_i^{\text{resolution}} \ge T_{k}^{\text{test\_start}}$$
   - **Forward-Price Microstructure Labeling**:
     Fixed timedelta buffers (e.g. 15–25 min) are strictly prohibited because candle intervals can vary or contain collection gaps, causing the $H$-th observed candle to occur later than a fixed delta.
     `build_labeled_frame()` explicitly records `resolution_timestamp` as the exact timestamp of the **decisive candle that finalized the bidirectional label**:
     - **Decisive Win**: If either direction hits its target (+15 pts for bull or -15 pts for bear before hitting stop), `resolution_timestamp` is the timestamp of the candle where that winning target was reached.
     - **Irreversible Double Stop**: If both directions hit their stop (+10/-10 pts) before any target is reached, `resolution_timestamp` is the timestamp of the candle where the second direction stopped out (the exact point at which neither direction can ever win).
     - **Horizon Expiry (Chop or Single Stop)**: If neither side hits target and at most one side stops out, the final label (0 for one-sided stop without target, or -1 for chop) is only finalized at the horizon boundary; `resolution_timestamp` is therefore the timestamp of the horizon candle ($H$-th candle or end of trading day).
     Under no circumstances may an intermediate, non-decisive single-side stop candle be recorded as the resolution timestamp while the opposing direction remains active and capable of reaching a winning target on subsequent candles.
     $$t_i^{\text{resolution}} = \text{resolution\_timestamp}_i$$
   - **Realized Trades**:
     $$t_i^{\text{resolution}} = \text{exit\_timestamp}_i$$
4. **Embargo Barrier**:
   In rolling window evaluations, observations occurring within an embargo buffer $h_{\text{embargo}}$ immediately following $T_{k}^{\text{test\_end}}$ are excluded from subsequent training folds to eliminate autoregressive serial correlation.

### 3.2 Statistical Confidence Interval & Metrics Reporting
For each fold $k$, the validator computes:
- Sample counts ($N_{\text{train}}, N_{\text{test}}$) and class balance ($p_{\text{pos\_train}}, p_{\text{pos\_test}}$).
- Fold metrics: $\text{AUC}_k, \text{Brier}_k, \text{Precision}_k, \text{Recall}_k, \text{F1}_k, \text{Precision@Top20}_k$.
- Sharpe ratio on out-of-fold test trades.

Across all $K$ folds:
- **Mean Metric**:
  $$\bar{\mu} = \frac{1}{K}\sum_{k=1}^K m_k$$
- **Sample Standard Deviation & Standard Error**:
  $$s = \sqrt{\frac{1}{K-1}\sum_{k=1}^K (m_k - \bar{\mu})^2}, \quad \text{SE} = \frac{s}{\sqrt{K}}$$
- **95% Confidence Interval (Student's $t$-distribution with $K-1$ degrees of freedom)**:
  $$\text{CI}_{95\%} = \left[\bar{\mu} - t_{0.975, K-1} \cdot \text{SE}, \; \bar{\mu} + t_{0.975, K-1} \cdot \text{SE}\right]$$
  Here $t_{\text{crit}} = t_{0.975, K-1} = |t_{0.025, K-1}| > 0$ denotes the positive two-tailed 97.5th-percentile critical value (e.g., `scipy.stats.t.ppf(0.975, df=evaluable_folds - 1)`). This guarantees that the lower bound $\text{CI}_{95\%\text{-lower}} = \bar{\mu} - t_{\text{crit}} \cdot \text{SE}$ is strictly less than the mean $\bar{\mu}$, preventing inverted endpoint definitions.
- **Pooled Out-of-Fold (OOF) Prediction**:
  Concatenate out-of-fold predictions $\{(\hat{y}_i, y_i)\}_{i=1}^N$ to compute global out-of-fold $\text{AUC}_{\text{OOF}}$ and $\text{Brier}_{\text{OOF}}$.

---

## 4. Strict Data Leakage Guards (`ml_signal/leakage_guards.py`)

The pipeline must enforce four hard invariant guards. Any violation immediately raises `DataLeakageError` and halts training:

### 4.1 Invariant 1: Outcome Field Leakage Guard
Features supplied to the model must be strictly disjoint from all outcome, target, and telemetry columns:
```python
FORBIDDEN_OUTCOME_FIELDS = {
    "label", "target", "trade_id", "trade_outcome", "trade_pnl",
    "pnl_points", "exit_timestamp", "exit_price", "exit_type",
    "result_state", "realized_pnl", "duration_seconds", "close",
    "raw_candle", "pnl", "pnl_amount", "stop_loss_hit", "target_hit",
    "resolution_timestamp", "signal_id", "signal_setup_type"
}
```
**Assertion**: `assert set(feature_cols).isdisjoint(FORBIDDEN_OUTCOME_FIELDS)`

### 4.2 Invariant 2: Lookahead & Chronological Order Guard
1. Timestamps within every training and evaluation frame must be strictly monotonic increasing:
   `assert df["timestamp"].is_monotonic_increasing`
2. Train and test splits must have strictly non-overlapping, chronologically ordered timestamps:
   `assert train_df["timestamp"].max() < test_df["timestamp"].min()`

### 4.3 Invariant 3: Forward Horizon & Trade Window Purging Guard
For every training observation $i$, its actual label resolution timestamp must strictly precede the earliest timestamp in the evaluation set:
- Unified Resolution Timestamp Check (both pipelines):
  `assert pd.to_datetime(train_df["resolution_timestamp"]).max() < test_df["timestamp"].min()`
  *(Where `resolution_timestamp` is the barrier-hit candle timestamp in `MarketMovementPipeline`, and `exit_timestamp` in `TradeOutcomePipeline`).*

### 4.4 Invariant 4: Duplicate Snapshot Deduplication & Feature Hygiene
To prevent identical market states from polluting multiple folds:
- **Schema & Fetcher Additions**: Update `_fetch_ml_collection()` in `ml_signal/train_offline.py` to explicitly query `signal_id` and `signal_setup_type` (the canonical column names in `ml_collection` schema).
- **Timestamp Bucketing & Post-Flattening Deduplication**:
  Because `main.py` records polling snapshots using execution time (`now = datetime.now()`), two polling snapshots within the same 1-minute candle cycle (e.g. `10:00:05` and `10:00:55`) have slightly different timestamps despite identical market features.
  Therefore, deduplication is executed immediately upon flattening rows (in `flatten_features()` / `prepare_dataset()`):
  1. Floor snapshot timestamps to the canonical 1-minute candle boundary:
     `df["bucketed_timestamp"] = pd.to_datetime(df["timestamp"]).dt.floor("1min")`
     (or use the nested candle open timestamp from `raw_candle`).
  2. Apply deduplication on composite keys using the bucketed timestamp:
     - Primary composite key: `["bucketed_timestamp", "signal_id", "signal_setup_type"]` (for signal-associated rows).
     - Unsignaled background snapshots fallback: `["bucketed_timestamp", "close"]` where `close` is the numeric spot close price extracted from `raw_candle`.
  3. Drop temporary helper `bucketed_timestamp` immediately after deduplication.
- **Metadata Exclusion & Feature Hygiene**: Tracking identifiers and partition columns (`signal_id`, `signal_setup_type`, and `resolution_timestamp`) are strictly tracking metadata. They must be registered in `_META_COLS` in `ml_signal/dataset.py` so `feature_columns(df)` excludes them from model inputs, and dropped or filtered before constructing the feature matrix $X$. Under no circumstances may raw UUIDs or categorical signal identities pass into XGBoost/LightGBM.
- Log the count of dropped duplicate snapshots and assert deduplication prior to time-series fold partitioning.

---

## 5. Sample Size Evaluation ($N = 232$) & Hybrid Transfer Architecture

### 5.1 Evaluation: Is $N = 232$ Sufficient for Standalone Modeling?
- **Conclusion**: **NO. $N = 232$ is statistically insufficient for standalone high-capacity XGBoost modeling.**
- **Empirical & Theoretical Rationale**:
  1. **Degrees of Freedom & Overfitting**: With 232 total samples (75 wins, 157 losses), an 80% train split has only 60 positive cases. A standard gradient boosted tree with 40–50 features has far more degrees of freedom than sample events ($E/P \approx 1.2$, far below the recommended minimum of $10\text{--}20$).
  2. **Empirical Evidence**: `v8_offline_metrics.json` recorded an AUC of 0.418 and F1 of 0.214 on held-out test trades.
  3. **Regime Vulnerability**: 232 trades span ~40 active days. Macro drift between month 1 and month 2 causes gradient updates to fit noise rather than invariant structural edge.

### 5.2 Two-Stage Hybrid Transfer Learning Architecture & Live Serving Contract
Until realized trade records reach $N \ge 1,000$ (expected Q1 2027), ARES adopts a **Two-Stage Hybrid Transfer Model**:

1. **Stage 1: Pretrained Market Dynamics Representation**:
   - Train a foundational representation model on the large continuous snapshot dataset ($N = 14,823$ rows, $N = 617$ resolved moves with `market_movement_tp_points = 15.0`, `market_movement_sl_points = 10.0`).
   - **Market-Only Feature Scope**: Excludes `detector_scores` from Stage 1 input columns. Trained strictly on continuous market dynamics: `candle_features`, `volume_features`, `iv_features`, `oi_features`, `greek_features`, `structure_features`, and `meta_features` (plus structural indicators). This aligns `stage1_feature_names` 100% with the features produced by `build_feature_vector()` in live serving, eliminating any missing or NaN detector columns during inference.
   - Learns non-linear interactions across Open Interest walls, IV skew, volume acceleration, and candle structure.
2. **Stage 2: Transfer / Feature Stacking into Trade Outcome Classifier**:
   - **Cross-Fitting Invariant (Zero-Leakage Transfer Prior & Out-of-Fold Stacking)**:
     Passing a single statically pretrained Stage 1 model directly into trade-outcome validation causes fatal lookahead leakage. Furthermore, using a single Stage 1 model trained on all snapshots up to $T_k^{\text{test\_start}}$ to score both training and test rows in trade fold $k$ creates severe stacking leakage: early training trades would be scored by a Stage 1 model that observed future snapshot labels, allowing Stage 2 to train on in-sample/future-informed Stage 1 scores while being evaluated on genuinely out-of-sample scores.
     Therefore, during walk-forward validation of the hybrid model, Stage 1 predictions **MUST be cross-fitted strictly using rolling / inner-OOF scoring for outer training rows and cutoff scoring for outer test rows**:
     - For outer trade fold $k$ with test window $[T_k^{\text{test\_start}}, T_k^{\text{test\_end}}]$ and candidate training set $\mathcal{D}_k^{\text{train}}$:
       1. **Outer Test Rows Scoring**:
          Fit a Stage 1 market model strictly on snapshots where `resolution_timestamp < T_k^{\text{test\_start}}`. Use this model exclusively to compute `meta_features__market_movement_prob` for outer test rows in $[T_k^{\text{test\_start}}, T_k^{\text{test\_end}}]$.
       2. **Outer Train Rows Scoring (Inner Walk-Forward / Rolling OOF)**:
          For each trade $i \in \mathcal{D}_k^{\text{train}}$ entered at timestamp $t_i^{\text{entry}}$, compute its `meta_features__market_movement_prob` using a Stage 1 model trained strictly on historical snapshots resolved prior to that trade: `resolution_timestamp < t_i^{\text{entry}}` (generated via expanding-window / chronological inner-fold cross-fitting). Under no circumstances may a Stage 1 model trained on snapshots occurring after $t_i^{\text{entry}}$ score trade $i$.
     - Only after walk-forward cross-validation passes all promotion criteria are the final Stage 1 and Stage 2 models trained:
       - Stage 1 is trained on the complete snapshot history.
       - Stage 2 is trained on all valid training trades scored with rolling/OOF Stage 1 probabilities.
       - Both estimators and feature schemas are bundled into the deployable `HybridPredictorBundle`.
   - The Stage 2 Trade Outcome model is trained on the $N = 232$ trades with **heavily regularized, constrained hyperparameters**:
     - `max_depth = 2` (shallow stumps to prevent high-order interactions).
     - `n_estimators = 50`, `learning_rate = 0.03`.
     - `reg_lambda = 5.0`, `reg_alpha = 1.0` (L1/L2 shrinkage).
     - `colsample_bytree = 0.6`, `subsample = 0.7`.
     - Input features restricted to: `meta_features__market_movement_prob` + top 6 structural features.
   - **Leakage-Free Fold-Local Feature Selection**:
     Under no circumstances may full-history SHAP gain from a model trained on the entire dataset be used to select Stage 2 features during walk-forward cross-validation.
     - Inside each outer fold $k$, the 6 structural features must be selected strictly from the fold-specific Stage 1 model fitted on historical snapshots resolved prior to that fold (`resolution_timestamp < T_k^{\text{test\_start}}`), or fixed *a priori* to canonical structural features (`structure_features__dist_to_nearest_support`, `structure_features__dist_to_nearest_resistance`, `oi_features__pcr`, `candle_features__body_pct`, `iv_features__iv_level`, `greek_features__net_delta`).
     - Full-dataset SHAP importance ranking is performed strictly post-validation for the final promoted bundle and diagnostic reporting.
   - **Early Stopping Prohibition on Outer Test Windows**:
     Under no circumstances may outer test partition $[T_k^{\text{test\_start}}, T_k^{\text{test\_end}}]$ be supplied to `model.fit()` as `eval_set` or used for early stopping. Supplying test labels to select the stopping iteration leaks test performance into the model parameters and invalidates out-of-sample claims.
     - During outer walk-forward validation folds, early stopping must either be disabled (`early_stopping_rounds = None`) with fixed regularized iterations (`n_estimators = 50`), or an **inner chronological validation slice** (the tail 15–20% of the candidate training partition, purged against earlier train trades) must be used as `eval_set`.
     - Outer test rows are strictly scored post-fit via `predict_proba()`.
   - When sample size is $N < 500$, the training pipeline automatically tags the model as `provisional_sample_size: true`, emitting explicit warnings in metrics reports.

3. **Inference Serving Contract (`HybridPredictorBundle`)**:
   - **The Defect**: If Stage 2 is promoted with dependency on `meta_features__market_movement_prob` while inference callers only have raw market features, `SignalPredictor.predict_from_raw()` would default the transfer feature to `np.nan`, degrading live predictions.
   - **Unified Artifact Solution**:
     A promoted hybrid model is serialized as a self-contained `HybridPredictorBundle`:
     ```python
     @dataclass
     class HybridPredictorBundle:
         stage1_model: Any                      # Pretrained market movement classifier
         stage2_model: Any                      # Regularized trade outcome classifier
         stage1_feature_names: List[str]        # Exact input columns for Stage 1 (pure market features)
         stage2_feature_names: List[str]        # Input columns for Stage 2 (including transfer prior)
         model_version: str                     # e.g., "v10"
         created_at: str
         metrics_summary: Dict[str, Any]
     ```
   - **Live Prediction Flow in `SignalPredictor.predict_from_raw()`**:
     ```python
     if isinstance(self.model, HybridPredictorBundle):
         # Step 1: Compute Stage 1 market probability using pure market features
         # (all guaranteed present in raw_features from build_feature_vector())
         p_market = self.model.stage1_model.predict_proba(X_stage1)[:, 1]
         raw_features["meta_features__market_movement_prob"] = float(p_market[0])
         # Step 2: Compute Stage 2 trade execution outcome probability
         prob = float(self.model.stage2_model.predict_proba(X_stage2)[:, 1][0])
     else:
         # Standard standalone estimator (legacy compatibility)
         prob = float(self.model.predict_proba(X)[:, 1][0])
     ```
     This encapsulates both models within the canonical `models/v{N}.joblib` file, requiring zero changes from upstream execution callers while guaranteeing the transfer feature is faithfully computed at runtime without missing detector inputs.

---

## 6. Hard Production Promotion Gating Rule (`ml_signal/promotion_gate.py`)

To prevent premature promotion of overfitted or unrepresentative models into live trading alerts:

### 6.1 Gating Invariants
A newly trained model file **CANNOT** be promoted to active production (`ml_signal/models/v{N}.joblib`) unless ALL of the following criteria are satisfied:
1. **Validation Protocol**: Validation method must strictly be `walk_forward_purged`. Models trained on a single chronological or random split are blocked.
2. **Completed Evaluable Fold Quantity**:
   - `evaluable_folds >= 4` successfully completed with finite numeric metrics.
   - An evaluable fold requires both positive and negative classes in its test slice ($y_{\text{test}} \in \{0, 1\}$).
   - Folds with single-class test slices or non-finite metrics (`np.isnan` / undefined AUC) are flagged as degenerate; `degenerate_folds == 0` is strictly enforced.
3. **Discriminative Power**:
   - Out-of-fold Mean AUC $\ge 0.55$.
   - Lower bound of 95% Confidence Interval: $\text{AUC}_{95\%\text{-lower}} > 0.50$ (statistically significant outperformance vs random guessing).
4. **Regime Consistency (Max Fold Degradation)**:
   - Minimum fold AUC across all evaluable folds: $\min(\text{AUC}_k) \ge 0.40$ (prevents models that fail catastrophically in specific market regimes).
5. **Calibration & Reliability**:
   - Out-of-fold Brier Score $\le 0.23$.
6. **Data Leakage Compliance**:
   - `leakage_guard_passed == True`.

### 6.2 Failure Handling & CI Containment
If any gating rule fails:
- The script logs detailed failure reasons to `stdout` and writes the failure audit to `reports/ml/{version}_rejection_audit.json`.
- The unvetted model candidate is serialized strictly as `ml_signal/models/{version}_unpromoted.joblib` for research inspection, while canonical production pointer (`models/v{N}.joblib`) remains untouched.
- When `--enforce-gate` is specified (mandatory for scheduled CI workflows), the script raises `ModelPromotionError` and exits with non-zero exit code (1), immediately halting the CI job so downstream artifact commit and Fly.io deployment steps never execute.
- In all environments, automated git commit steps must exclude `*_unpromoted.joblib` and condition deployment strictly on verified canonical promotion (`promoted == true`).

---

## 7. Implementation Task Assignment for Code Generator Agent

Assign implementation of ticket **MANM-155** to the **Code Generator Agent** with the following explicit file tasks:

### Task Breakdown & Interface Contracts

#### 1. `ml_signal/leakage_guards.py` (New Module)
- Implement `assert_no_outcome_leakage(feature_names: List[str]) -> None`:
  - Validates feature columns against `FORBIDDEN_OUTCOME_FIELDS` (including `resolution_timestamp`, `signal_id`, and `signal_setup_type`).
- Implement `assert_chronological_integrity(df: pd.DataFrame, date_col: str = "timestamp") -> None`:
  - Checks `df[date_col].is_monotonic_increasing`.
- Implement `assert_train_test_purged(train_df: pd.DataFrame, test_df: pd.DataFrame, resolution_col: str = "resolution_timestamp") -> None`:
  - Verifies that train actual resolution timestamps strictly precede test start timestamps:
    `assert pd.to_datetime(train_df[resolution_col]).max() < pd.to_datetime(test_df["timestamp"]).min()`.
- Implement `deduplicate_snapshots(df: pd.DataFrame) -> pd.DataFrame`:
  - Floors timestamps to canonical 1-minute candle boundaries (`pd.to_datetime(df["timestamp"]).dt.floor("1min")`) to prevent sub-minute polling duplicates from surviving.
  - Deduplicates using composite key `["bucketed_timestamp", "signal_id", "signal_setup_type"]` for signaled rows and `["bucketed_timestamp", "close"]` for unsignaled cycle snapshots.
  - Drops deduplication keys (`bucketed_timestamp`, `signal_id`, `signal_setup_type`) immediately or ensures they are excluded from feature vectors via `_META_COLS`.
- Define custom exception `DataLeakageError(Exception)`.

#### 2. `ml_signal/validation.py` (New Module)
- Implement class `WalkForwardPurgedCV`:
  ```python
  class WalkForwardPurgedCV:
      def __init__(
          self,
          n_splits: int = 5,
          min_train_samples: int = 100,
          embargo_window: pd.Timedelta = pd.Timedelta(minutes=0),
      ): ...
      
      def split(
          self,
          df: pd.DataFrame,
          timestamp_col: str = "timestamp",
          resolution_col: str = "resolution_timestamp",
      ) -> Iterator[Tuple[np.ndarray, np.ndarray, Dict[str, Any]]]: ...
  ```
  - Purges any candidate train index where `df.loc[idx, resolution_col] >= test_start_timestamp`.
- Implement `compute_cv_metrics(fold_results: List[Dict[str, Any]], leakage_guard_passed: bool = True) -> Dict[str, Any]`:
  - Filters and records `evaluable_folds` (folds with both classes present in the test slice and finite metrics).
  - Flags `degenerate_folds` (single-class test partitions or NaN/undefined metrics).
  - Computes fold means, standard deviations, standard errors, and 95% Student's $t$ confidence intervals across evaluable folds using positive critical value $t_{\text{crit}} = \text{float}(\text{scipy.stats.t.ppf}(0.975, df=\text{evaluable\_folds} - 1))$, setting `ci_95_lower = mean - t_crit * se` and `ci_95_upper = mean + t_crit * se`.
  - Computes pooled out-of-fold (OOF) AUC, Brier, and LogLoss.
  - Records `leakage_guard_passed: bool` in returned metrics dictionary for evaluation by `promotion_gate.py`.

#### 3. `ml_signal/promotion_gate.py` (New Module)
- Implement `evaluate_promotion_gate(metrics: Dict[str, Any]) -> Tuple[bool, List[str]]`:
  - Validates:
    - `validation_method == "walk_forward_purged"`
    - `evaluable_folds >= 4` (must have at least 4 valid, non-degenerate evaluable folds)
    - `degenerate_folds == 0` (zero single-class or NaN-metric folds)
    - `mean_auc >= 0.55` and `ci_95_lower > 0.50`
    - `min_fold_auc >= 0.40`
    - `brier_score <= 0.23`
    - `leakage_guard_passed == True` (single unified boolean metric emitted by pipeline validation and required by gate)
- Implement `enforce_promotion_or_raise(metrics: Dict[str, Any]) -> None`:
  - Raises `ModelPromotionError` on failure.

#### 4. `ml_signal/pipeline_market_movement.py` (New Module) & `ml_signal/dataset.py`
- In `ml_signal/dataset.py`:
  - Register `signal_id`, `signal_setup_type`, and `resolution_timestamp` in `_META_COLS` so `feature_columns(df)` strictly excludes them from model inputs.
  - Update `build_labeled_frame` and `label_forward_points` to record and return `resolution_timestamp` for every labeled observation as the decisive resolution candle:
    - Target hit candle timestamp when bull or bear hits target (+15 pts).
    - Second stop candle timestamp when both directions hit stops (+10/-10 pts, irreversible stop).
    - Horizon candle timestamp when evaluation times out (chop or single-side stop).
- In `ml_signal/pipeline_market_movement.py`:
  - Implement `MarketMovementPipeline`:
    ```python
    class MarketMovementPipeline:
        def __init__(self, config: MLConfig = DEFAULT_CONFIG): ...
        def prepare_dataset(self, rows: List[dict]) -> pd.DataFrame: ...
        def run_walk_forward(self, df: pd.DataFrame, n_splits: int = 5) -> Tuple[object, Dict[str, Any]]: ...
    ```
    - Passes `tp_points=config.market_movement_tp_points, sl_points=config.market_movement_sl_points` into `build_labeled_frame()`.
    - **Market-Only Features (Zero Inference Mismatch)**: Restricts Stage 1 feature columns strictly to pure market groups (`FEATURE_GROUPS` excluding `detector_scores`). This guarantees `stage1_feature_names` is 100% covered by `build_feature_vector()` at runtime, with zero missing/NaN detector columns.
    - Runs `WalkForwardPurgedCV` with decisive resolution timestamp purging.
    - **No Outer Test Leaks in Early Stopping**: Outer test partition $[T_k^{\text{test\_start}}, T_k^{\text{test\_end}}]$ is strictly isolated from `model.fit()` (never passed as `eval_set`). Model fits either disable early stopping (`early_stopping_rounds=None`) with fixed estimators, or carve an inner chronological validation split strictly from `train_df`.
    - Outputs `market_movement` metrics and model.

#### 5. `ml_signal/pipeline_trade_outcomes.py` (New Module)
- Implement `TradeOutcomePipeline`:
  ```python
  class TradeOutcomePipeline:
      def __init__(self, config: MLConfig = DEFAULT_CONFIG, use_hybrid_transfer: bool = True): ...
      def prepare_dataset(self, rows: List[dict], exit_timestamps: Dict[str, str]) -> pd.DataFrame: ...
      def run_walk_forward(
          self,
          df: pd.DataFrame,
          market_snapshots_df: Optional[pd.DataFrame] = None,
          n_splits: int = 5
      ) -> Tuple[object, Dict[str, Any]]: ...
  ```
  - **Timestamp Anomaly Filtering**: In `prepare_dataset()`, strictly reject/exclude any trades where `time_metrics_excluded is True`, `exit_timestamp is None/NaT`, or `exit_timestamp <= entry_timestamp/timestamp` (preserving MANM-152 contracts). Only verified positive-duration trades enter training.
  - Maps valid `exit_timestamp` to `resolution_timestamp`.
  - **Fold-Level Cross-Fitting**: In `run_walk_forward()`, for each walk-forward fold $k$:
    - Outer test rows are scored using a Stage 1 model fitted strictly on snapshots with `resolution_timestamp < T_k^{test_start}`.
    - Outer train rows are scored using inner-OOF / rolling Stage 1 models fitted strictly on snapshots with `resolution_timestamp < t_entry`, preventing in-sample stacking bias and lookahead leakage.
  - **Fold-Local Feature Selection**: Selects Stage 2's top 6 structural features strictly from fold $k$'s pre-test Stage 1 model SHAP gain (or fixed *a priori* structural columns), prohibiting full-dataset lookahead in feature selection.
  - **Strict Early Stopping Prohibition**: Enforces `early_stopping_rounds=None` and fits regularized shallow trees (`max_depth=2`, `n_estimators=50`, L1/L2 shrinkage) directly on candidate train sets; outer test partitions are never passed as `eval_set`.
  - Runs `WalkForwardPurgedCV` with trade duration exit timestamp purging.
  - On successful promotion, fits final models on complete history (Stage 2 trained on rolling Stage 1 features) and packages `HybridPredictorBundle`.

#### 6. `ml_signal/train_offline.py` (Refactor CLI Entrypoint & Fetcher)
- Update `_fetch_ml_collection()` query to explicitly select `signal_id` and `signal_setup_type`.
- Add command-line argument parser:
  - `--pipeline [market_movement | trade_outcomes | all]` (default: `all`).
  - `--folds <int>` (default: 5).
  - `--promote / --no-promote` (default: `--promote`, strictly enforced by `promotion_gate.py`; `--no-promote` is opt-out for dry-runs/experiments).
  - `--enforce-gate / --no-enforce-gate` (default: `--no-enforce-gate` locally, `--enforce-gate` in CI workflows; raises `ModelPromotionError` and exits 1 on gate failure).
  - `--hybrid / --no-hybrid` (default: `--hybrid`).
  - `--metrics-path <str>` (default: `reports/ml/task183_offline_metrics.json` or `METRICS_PATH` env).
- **Single Production Target**: In `--pipeline all --promote`, strictly designate `TradeOutcomePipeline` (`HybridPredictorBundle`) as the sole target promoting to `ml_signal/models/v{N}.joblib`. Market movement model is preserved as auxiliary `models/market_movement_v{N}.joblib`.
- **Primary Report Legacy Schema Preservation**:
  Write primary summary report to `reports/ml/task183_offline_metrics.json` preserving all top-level keys expected by `.github/workflows/ml_training.yml:73-82`:
  `model_version`, `auc_roc` (mapped from `mean_auc`), `sharpe_status`, `sharpe_annualized`, `sharpe_daily`, `sharpe_days`, `sharpe_trades`, `sharpe_window_start`, `sharpe_window_end`, `sharpe_total_pnl_points`, `shap_status`, `shap_computed`, `promoted`, and `leakage_guard_passed`, preventing `n/a` values in weekly Discord reports and job summaries. Also outputs diagnostic JSONs for each pipeline.

#### 7. `ml_signal/predictor.py` (Inference Serving Support)
- Define `HybridPredictorBundle` dataclass.
- Update `SignalPredictor.predict_from_raw()`:
  - Detects if `self.model` is `HybridPredictorBundle`.
  - If hybrid: runs Stage 1 to generate `meta_features__market_movement_prob` using pure market features from `build_feature_vector()`, adds it to feature dict, and evaluates Stage 2.
  - If standard estimator: evaluates directly (preserving full backward compatibility for legacy models `v1`–`v9`).

#### 8. Unit Tests (`tests/unit/test_task155_ml_training_refactor.py` & `tests/unit/test_ml_predictor.py`)
- Test walk-forward purge mechanism using actual resolution timestamps across gaps and non-uniform candles.
- Test rejection of anomaly trades (`time_metrics_excluded = True` or inverted exit timestamps).
- Test fold-level cross-fitting of Stage 1 transfer feature preventing future-fold lookahead.
- Test that `stage1_feature_names` excludes `detector_scores` and is 100% satisfied by `build_feature_vector()`.
- Test that Stage 2 structural feature selection inside walk-forward CV is strictly fold-local with zero full-dataset lookahead.
- Test data leakage guards (assert `FORBIDDEN_OUTCOME_FIELDS` raises `DataLeakageError`).
- Test promotion gate on evaluable vs degenerate single-class folds (`degenerate_folds > 0` raises `ModelPromotionError`).
- Test single production target rule (market-movement model does not overwrite `models/v{N}.joblib`).
- Test `SignalPredictor.predict_from_raw()` with `HybridPredictorBundle` ensuring `meta_features__market_movement_prob` is computed dynamically at inference time without missing feature warnings.

#### 9. Scheduled Training Workflow Migration (`.github/workflows/ml_training.yml`)
- Update `Run offline ML model training` step to run with gate enforcement:
  `python -m ml_signal.train_offline --promote --enforce-gate`
  This ensures any promotion gate failure causes the training step to exit with code 1, immediately halting the workflow and preventing downstream artifact commit or deployment.
- Update `Commit and push updated model artifact to main` step:
  - Exclude rejection artifacts: strictly stage canonical models and metrics via `git add ml_signal/models/v[0-9]*.joblib "$METRICS_PATH"` (explicitly never staging `*_unpromoted.joblib` or rejection audits).
  - Add explicit guard: check that candidate was promoted via `test "$(jq -r '.promoted // false' "$METRICS_PATH")" = "true"`. If false or gate failed, skip commit.
- Update `deploy` job:
  - Condition deployment strictly on successful promotion by adding output `promoted` from the `train` job:
    `if: (github.ref == 'refs/heads/main' || github.event_name == 'schedule') && needs.train.outputs.promoted == 'true'`
    This guarantees unpromoted models or failed training runs can never trigger a Fly.io deployment.

---

## 8. Definition of Done & Quality Gate

1. **Decoupled Scripts**: `MarketMovementPipeline` and `TradeOutcomePipeline` operate independently and can be run in isolation or sequentially.
2. **Walk-Forward Validation**: Cross-validation produces complete fold-by-fold breakdowns, sample counts, class distributions, and 95% confidence intervals across $\ge 4$ evaluable folds.
3. **Data Leakage Guards**: Rigorous runtime assertion checks verify zero outcome leakage, zero lookahead bias, and zero overlapping trade evaluation windows using actual label resolution timestamps.
4. **Promotion Gating**: A hard gate in code prohibits saving or promoting models based on a single chronological split or non-performing walk-forward metrics. Promoted models conform to `models/v{N}.joblib`.
5. **Workflow Safety & Containment**: Scheduled workflows run `train_offline --promote --enforce-gate`, exclude `_unpromoted.joblib` from git staging, and gate Fly.io deployments strictly on verified promotion, preventing unvetted models from being committed or deployed.
6. **Inference Consistency**: Promoted hybrid models generate the `meta_features__market_movement_prob` transfer feature dynamically at serving time via `HybridPredictorBundle`.
7. **No Production Code Direct Modification**: Software Architect produces ADR only; implementation handed off to Code Generator Agent.
