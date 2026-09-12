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

### 2.1 Component Decoupling
To achieve complete architectural decoupling without breaking external callers:
1. **Market Movement Pipeline (`ml_signal/pipeline_market_movement.py`)**:
   - Encapsulates dataset preparation via `build_labeled_frame`.
   - Focuses strictly on market microstructure dynamics (predicting whether price moves $\ge 15$ points before hitting $-10$ points stop within 5 candles).
   - Artifacts: `ml_signal/models/market_movement_v{N}.joblib`, `reports/ml/market_movement_metrics.json`.
2. **Realized Trade Outcome Pipeline (`ml_signal/pipeline_trade_outcomes.py`)**:
   - Encapsulates dataset preparation via `build_real_outcome_frame`.
   - Focuses strictly on execution reliability given signal entry conditions.
   - Evaluates whether execution sample size is sufficient or if hybrid transfer is activated.
   - Artifacts: `ml_signal/models/trade_outcome_v{N}.joblib`, `reports/ml/trade_outcome_metrics.json`.
3. **Unified CLI Orchestrator (`ml_signal/train_offline.py`)**:
   - Serves as the high-level entrypoint supporting CLI arguments:
     `python -m ml_signal.train_offline --pipeline [market_movement | trade_outcomes | all] --folds 5 --promote`.

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
3. **Purging Barrier**:
   An observation $i$ in the candidate training set is **purged** if its forward evaluation horizon or trade duration overlaps the test start:
   $$\text{Purge Condition: } t_i^{\text{end}} \ge T_{k}^{\text{test\_start}}$$
   - For forward-price labeling (lookforward $H = 5$ candles): $t_i^{\text{end}} = t_i^{\text{start}} + H \cdot \Delta t_{\text{candle}}$.
   - For realized trades: $t_i^{\text{end}} = \text{exit\_timestamp}_i$.
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
  $$\text{CI}_{95\%} = \left[\bar{\mu} - t_{0.025, K-1} \cdot \text{SE}, \; \bar{\mu} + t_{0.025, K-1} \cdot \text{SE}\right]$$
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
    "raw_candle", "pnl", "pnl_amount", "stop_loss_hit", "target_hit"
}
```
**Assertion**: `assert set(feature_cols).isdisjoint(FORBIDDEN_OUTCOME_FIELDS)`

### 4.2 Invariant 2: Lookahead & Chronological Order Guard
1. Timestamps within every training and evaluation frame must be strictly monotonic increasing:
   `assert df["timestamp"].is_monotonic_increasing`
2. Train and test splits must have strictly non-overlapping, chronologically ordered timestamps:
   `assert train_df["timestamp"].max() < test_df["timestamp"].min()`

### 4.3 Invariant 3: Forward Horizon & Trade Window Purging Guard
For every training observation $i$, its target resolution timestamp must strictly precede the earliest timestamp in the evaluation set:
- Forward price pipeline:
  `assert (train_df["timestamp"] + pd.to_timedelta(lookforward_minutes, unit="m")).max() < test_df["timestamp"].min()`
- Realized trade pipeline:
  `assert pd.to_datetime(train_df["exit_timestamp"]).max() < test_df["timestamp"].min()`

### 4.4 Invariant 4: Duplicate Snapshot Deduplication
To prevent identical market states from polluting multiple folds:
- Deduplicate input rows on composite key `("timestamp", "signal_id", "setup_type")` (or `("timestamp", "close")` if signal id is absent).
- Log count of dropped duplicate snapshots before feature extraction.

---

## 5. Sample Size Evaluation ($N = 232$) & Hybrid Transfer Architecture

### 5.1 Evaluation: Is $N = 232$ Sufficient for Standalone Modeling?
- **Conclusion**: **NO. $N = 232$ is statistically insufficient for standalone high-capacity XGBoost modeling.**
- **Empirical & Theoretical Rationale**:
  1. **Degrees of Freedom & Overfitting**: With 232 total samples (75 wins, 157 losses), an 80% train split has only 60 positive cases. A standard gradient boosted tree with 40–50 features has far more degrees of freedom than sample events ($E/P \approx 1.2$, far below the recommended minimum of $10\text{--}20$).
  2. **Empirical Evidence**: `v8_offline_metrics.json` recorded an AUC of 0.418 and F1 of 0.214 on held-out test trades.
  3. **Regime Vulnerability**: 232 trades span ~40 active days. Macro drift between month 1 and month 2 causes gradient updates to fit noise rather than invariant structural edge.

### 5.2 Two-Stage Hybrid Transfer Learning Architecture
Until realized trade records reach $N \ge 1,000$ (expected Q1 2027), ARES adopts a **Two-Stage Hybrid Transfer Model**:

1. **Stage 1: Pretrained Market Dynamics Representation**:
   - Train a foundational model on the large self-labeled continuous snapshot dataset ($N = 14,823$ rows, $N = 617$ resolved moves).
   - Learns deep non-linear interactions between Open Interest walls, IV skew, volume acceleration, and candle structure.
2. **Stage 2: Transfer / Feature Stacking into Trade Outcome Classifier**:
   - The Stage 1 model generates an out-of-fold scalar feature: `meta_features__market_movement_prob`.
   - The Stage 2 Trade Outcome model is trained on the $N = 232$ trades with **heavily regularized, constrained hyperparameters**:
     - `max_depth = 2` (shallow stumps to prevent high-order interactions).
     - `n_estimators = 50`, `learning_rate = 0.03`.
     - `reg_lambda = 5.0`, `reg_alpha = 1.0` (L1/L2 shrinkage).
     - `colsample_bytree = 0.6`, `subsample = 0.7`.
     - Input features restricted to: `meta_features__market_movement_prob` + top 6 structural features selected by Stage 1 SHAP gain.
   - When sample size is $N < 500$, the training pipeline automatically tags the model as `provisional_sample_size: true`, emitting explicit warnings in metrics reports.

---

## 6. Hard Production Promotion Gating Rule (`ml_signal/promotion_gate.py`)

To prevent premature promotion of overfitted or unrepresentative models into live trading alerts:

### 6.1 Gating Invariants
A newly trained model file **CANNOT** be promoted to active production (`ml_signal/models/v{N}.joblib`) unless ALL of the following criteria are satisfied:
1. **Validation Protocol**: Validation method must strictly be `walk_forward_purged`. Models trained on a single chronological or random split are blocked.
2. **Fold Quantity**: $K \ge 4$ folds completed successfully.
3. **Discriminative Power**:
   - Out-of-fold Mean AUC $\ge 0.55$.
   - Lower bound of 95% Confidence Interval: $\text{AUC}_{95\%\text{-lower}} > 0.50$ (statistically significant outperformance vs random guessing).
4. **Regime Consistency (Max Fold Degradation)**:
   - Minimum fold AUC across all folds: $\min(\text{AUC}_k) \ge 0.40$ (prevents models that fail catastrophically in specific market regimes).
5. **Calibration & Reliability**:
   - Out-of-fold Brier Score $\le 0.23$.
6. **Data Leakage Compliance**:
   - `leakage_guard_passed == True`.

### 6.2 Failure Handling
If any gating rule fails:
- The script logs detailed failure reasons to `stdout` and writes the failure audit to `reports/ml/{version}_rejection_audit.json`.
- The model artifact is written with suffix `_unpromoted.joblib` for research inspection.
- The active model pointer (`models/v{N}.joblib`) remains untouched.
- If executed in an automated CI/deployment pipeline with `--enforce-gate`, the script exits with non-zero exit code (1).

---

## 7. Implementation Task Assignment for Code Generator Agent

Assign implementation of ticket **MANM-155** to the **Code Generator Agent** with the following explicit file tasks:

### Task Breakdown & Interface Contracts

#### 1. `ml_signal/leakage_guards.py` (New Module)
- Implement `assert_no_outcome_leakage(feature_names: List[str]) -> None`:
  - Validates feature columns against `FORBIDDEN_OUTCOME_FIELDS`.
- Implement `assert_chronological_integrity(df: pd.DataFrame, date_col: str = "timestamp") -> None`:
  - Checks `df[date_col].is_monotonic_increasing`.
- Implement `assert_train_test_purged(train_df: pd.DataFrame, test_df: pd.DataFrame, horizon_col: Optional[str], default_lookforward_minutes: int) -> None`:
  - Verifies that train horizon does not overlap test start timestamp.
- Implement `deduplicate_snapshots(df: pd.DataFrame, subset: List[str]) -> pd.DataFrame`.
- Define custom exception `DataLeakageError(Exception)`.

#### 2. `ml_signal/validation.py` (New Module)
- Implement class `WalkForwardPurgedCV`:
  ```python
  class WalkForwardPurgedCV:
      def __init__(
          self,
          n_splits: int = 5,
          min_train_samples: int = 100,
          purge_window: pd.Timedelta = pd.Timedelta(minutes=15),
          embargo_window: pd.Timedelta = pd.Timedelta(minutes=0),
      ): ...
      
      def split(
          self,
          df: pd.DataFrame,
          timestamp_col: str = "timestamp",
          exit_col: Optional[str] = None,
      ) -> Iterator[Tuple[np.ndarray, np.ndarray, Dict[str, Any]]]: ...
  ```
- Implement `compute_cv_metrics(fold_results: List[Dict[str, Any]]) -> Dict[str, Any]`:
  - Computes fold means, standard deviations, standard errors, and 95% Student's $t$ confidence intervals.
  - Computes pooled out-of-fold (OOF) AUC, Brier, and LogLoss.

#### 3. `ml_signal/promotion_gate.py` (New Module)
- Implement `evaluate_promotion_gate(metrics: Dict[str, Any]) -> Tuple[bool, List[str]]`:
  - Checks: `validation_method == "walk_forward_purged"`, `n_splits >= 4`, `mean_auc >= 0.55`, `ci_95_lower > 0.50`, `min_fold_auc >= 0.40`, `brier_score <= 0.23`, `leakage_clean == True`.
- Implement `enforce_promotion_or_raise(metrics: Dict[str, Any]) -> None`:
  - Raises `ModelPromotionError` on failure.

#### 4. `ml_signal/pipeline_market_movement.py` (New Module)
- Implement `MarketMovementPipeline`:
  ```python
  class MarketMovementPipeline:
      def __init__(self, config: MLConfig = DEFAULT_CONFIG): ...
      def prepare_dataset(self, rows: List[dict]) -> pd.DataFrame: ...
      def run_walk_forward(self, df: pd.DataFrame, n_splits: int = 5) -> Tuple[object, Dict[str, Any]]: ...
  ```
  - Prepares dataset using `build_labeled_frame`.
  - Runs `WalkForwardPurgedCV` with 5-candle purge window.
  - Outputs `market_movement` metrics and model.

#### 5. `ml_signal/pipeline_trade_outcomes.py` (New Module)
- Implement `TradeOutcomePipeline`:
  ```python
  class TradeOutcomePipeline:
      def __init__(self, config: MLConfig = DEFAULT_CONFIG, use_hybrid_transfer: bool = True): ...
      def prepare_dataset(self, rows: List[dict], exit_timestamps: Dict[str, str]) -> pd.DataFrame: ...
      def run_walk_forward(self, df: pd.DataFrame, n_splits: int = 5, market_model: Optional[object] = None) -> Tuple[object, Dict[str, Any]]: ...
  ```
  - Prepares dataset using `build_real_outcome_frame`.
  - Injects Stage 1 probability prior if `use_hybrid_transfer` is enabled.
  - Enforces shallow depth (`max_depth=2`) and L1/L2 shrinkage for small $N = 232$.
  - Runs `WalkForwardPurgedCV` with trade duration exit timestamp purging.

#### 6. `ml_signal/train_offline.py` (Refactor CLI Entrypoint)
- Add command-line argument parser:
  - `--pipeline [market_movement | trade_outcomes | all]` (default: `all`).
  - `--folds <int>` (default: 5).
  - `--promote / --no-promote` (default: `--no-promote` unless explicit).
  - `--hybrid / --no-hybrid` (default: `--hybrid`).
- Call `leakage_guards`, run designated pipelines, evaluate `promotion_gate`, and generate structured reports in `reports/ml/`.

#### 7. Unit Tests (`tests/unit/test_task155_ml_training_refactor.py`)
- Test walk-forward purge mechanism (assert zero overlap between train exit times and test start).
- Test data leakage guards (assert `FORBIDDEN_OUTCOME_FIELDS` raises `DataLeakageError`).
- Test promotion gate (assert single-split or AUC < 0.55 raises `ModelPromotionError`).
- Test hybrid transfer feature stacking on synthetic small-sample trade records.

---

## 8. Definition of Done & Quality Gate

1. **Decoupled Scripts**: `MarketMovementPipeline` and `TradeOutcomePipeline` operate independently and can be run in isolation or sequentially.
2. **Walk-Forward Validation**: Cross-validation produces complete fold-by-fold breakdowns, sample counts, class distributions, and 95% confidence intervals.
3. **Data Leakage Guards**: Rigorous runtime assertion checks verify zero outcome leakage, zero lookahead bias, and zero overlapping trade evaluation windows.
4. **Promotion Gating**: A hard gate in code prohibits saving or promoting models based on a single chronological split or non-performing walk-forward metrics.
5. **No Production Code Direct Modification**: Software Architect produces ADR only; implementation handed off to Code Generator Agent.
