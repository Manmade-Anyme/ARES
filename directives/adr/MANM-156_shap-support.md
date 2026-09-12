# ADR-156: Enable SHAP Package Support, Interpretability Reporting, Native TreeSHAP Fallback, and Feature Drift Stability Auditing

- **Status**: Proposed
- **Date**: 2026-09-12
- **Task ID**: MANM-156
- **Author**: Software Architect Agent (`d2d4e328-096d-4658-8d90-44aa7b51ed05`)
- **Parent Issue**: MANM-149 (`ARES: Supabase Backtesting Audit Remediation`)
- **PR**: [#111](https://github.com/Manmade-Anyme/ARES/pull/111) (`feature/MANM-156-shap-support`)

---

## 1. Executive Summary & Problem Diagnosis

### Problem Statement
During the ARES Supabase backtesting audit (2026-09-11), SHAP (SHapley Additive exPlanations) could not be calculated in the evaluation environment because the external Python `shap` package was missing from root `requirements.txt`. While the codebase contained a nascent native XGBoost fallback using `pred_contribs=True`, an explicit short-circuit in `ml_signal/train_offline.py` (`if exc.name == "shap": return`) aborted execution upon catching `ModuleNotFoundError`, skipping the fallback entirely. As a result:
1. Offline evaluation degraded solely to XGBoost global split gain. Global gain provides only feature-level impurity reduction and completely lacks local sample-level attribution, directional sign (positive vs. negative contribution to win probability), or raw-margin additivity.
2. In historical model v6, native XGBoost tree contributions on 42 holdout rows successfully isolated key drivers (time of day, candle body-to-range ratio, IV, volume ratio). However, without consistent package installation and resilient fallback execution, this interpretability capability was lost in audit and container environments.
3. Visualization was restricted to a simple horizontal bar chart of mean absolute SHAP values; no beeswarm distribution plots were generated to visualize feature value spread and directional impact.
4. No automated stability audit existed to detect feature attribution drift across rolling training windows or market regimes.
5. SHAP evaluation outputs lacked rich metadata tracing model version, training/test date windows, sample size, and feature schema versioning.

### Acceptance Criteria (from PM Directive `directives/MANM-156_shap_support.md`)
- Ensure `shap` package is specified in dependencies and installed in training/evaluation environments.
- Implement and preserve a graceful fallback to native XGBoost tree SHAP (`pred_contribs=True`) if the external `shap` package fails.
- Validate raw-margin / log-odds additivity for tree explainers.
- Generate holdout SHAP summary plots, beeswarm plots, and feature importance rankings.
- Audit SHAP stability across rolling training windows to detect feature drift.
- Ensure SHAP outputs capture metadata: model version, training/testing date windows, sample size, and feature schema version.

---

## 2. Architectural Decisions & System Design

```
+-------------------------------------------------------------------------------------------------+
|                                 ARES ML Training Pipeline                                       |
|                                                                                                 |
|   +--------------------------+        +--------------------------+                              |
|   |   Chronological Split    | -----> |   XGBClassifier Train    |                              |
|   |  X_train, y_tr / y_val   |        |  (best_iteration tuned)  |                              |
|   +--------------------------+        +--------------------------+                              |
|                                                     |                                           |
|                                                     v                                           |
|                                 +---------------------------------------+                       |
|                                 |    Interpretability Engine (SHAP)     |                       |
|                                 +---------------------------------------+                       |
|                                                     |                                           |
|                     +-------------------------------+-------------------------------+           |
|                     |                                                               |           |
|                     v [Tier 1: Preferred]                                           v [Tier 2: Fallback]
|         +-----------------------+                                       +-----------------------+
|         | import shap           |                                       | XGBoost Native        |
|         | shap.TreeExplainer    | -- (on ModuleNotFoundError / fail) -> | booster.predict(      |
|         | check_additivity=True |                                       |   pred_contribs=True) |
|         +-----------------------+                                       +-----------------------+
|                     |                                                               |           |
|                     +-------------------------------+-------------------------------+           |
|                                                     |                                           |
|                                                     v                                           |
|                                 +---------------------------------------+                       |
|                                 |    Unified Array Normalization        |                       |
|                                 |       Shape: (N_test, M_features)     |                       |
|                                 |     Unit: Raw Margin (Log-Odds)       |                       |
|                                 +---------------------------------------+                       |
|                                                     |                                           |
|         +-----------------------+-------------------+-----------------------+                   |
|         |                       |                   |                       |                   |
|         v                       v                   v                       v                   |
|  +--------------+       +---------------+   +---------------+       +---------------+           |
|  | Additivity   |       | Top-N Feature |   | Visualizations|       | Rolling Window|           |
|  | Verification |       | Importance    |   | - Summary Bar |       | Drift Audit   |           |
|  | sum(phi)+b=z |       | Mean |SHAP|   |   | - Beeswarm    |       | Rank Corr /   |           |
|  |              |       | Ranking       |   |   (SHAP / MPL)|       | Turnover Rate |           |
|  +--------------+       +---------------+   +---------------+       +---------------+           |
|                                                     |                                           |
|                                                     v                                           |
|                                 +---------------------------------------+                       |
|                                 |  Enriched JSON Report & Metadata      |                       |
|                                 |  (dates, sample sizes, schema hash)   |                       |
|                                 +---------------------------------------+                       |
+-------------------------------------------------------------------------------------------------+
```

### 2.1 Dependency Isolation & Environment Alignment
- **Root `requirements.txt`**: Add `shap>=0.47.0,<0.50.0` and `matplotlib>=3.7.0,<4.0.0` under an explicit ML Interpretability section.
- **Module Requirements**: Keep `ml_signal/requirements.txt` synchronized.
- **Live Serving Boundary**: In live trading execution (`ml_signal/predictor.py` and `main.py`), `shap` is **never imported** at module scope or during the sub-second tick loop. Live scoring relies strictly on `booster.predict_proba()` via Joblib-unpickled models, keeping runtime memory overhead minimal and startup latency instantaneous. All SHAP, plotting, and stability calculations remain strictly in `ml_signal/train_offline.py` and evaluation modules.

### 2.2 Resilient Multi-Tier Explanation Architecture
To guarantee that offline training and audit environments *never* lose feature interpretability:

1. **Tier 1 — Preferred External Explainer (`shap_tree_explainer`)**:
   - Attempt `import shap`.
   - Instantiate `explainer = shap.TreeExplainer(model, feature_perturbation="tree_path_dependent")`.
   - Calculate SHAP values passing `tree_limit=best_iteration + 1` and `check_additivity=True`.
   - Normalize output: Handle multidimensional or list outputs across SHAP version variants (extracting index `1` for the positive class in binary classification) into a canonical 2D NumPy array of shape `(len(X_test), len(feature_cols))`.
   - Tag backend as `"shap_tree_explainer"`.

2. **Tier 2 — Native XGBoost Exact TreeSHAP Fallback (`xgboost_pred_contribs`)**:
   - Triggered automatically if:
     - `import shap` raises `ModuleNotFoundError` (regardless of `exc.name`),
     - `shap` raises an `ImportError` or C-extension linking error,
     - `shap.TreeExplainer` throws any runtime or dimension mismatch exception.
   - Record `metrics["shap_fallback_reason"] = f"{type(exc).__name__}: {str(exc).strip()[:200]}"`.
   - Call `_native_shap_values(model, X_test, best_iteration)`.
   - Execute `booster.predict(dtest, pred_contribs=True, iteration_range=(0, best_iteration + 1))`.
   - Extract feature contributions from the returned matrix of shape `(N, M + 1)`:
     - Feature SHAP values: `contributions[:, :-1]` (columns $0$ through $M-1$).
     - Expected value (bias): `contributions[:, -1]` (column $M$).
   - Validate finite numbers (`np.isfinite`) and dimension conformity.
   - Tag backend as `"xgboost_pred_contribs"`.
   - Populate `metrics["shap_status"] = "computed"`, `metrics["shap_computed"] = True`, and calculate top features identically to Tier 1.

3. **Tier 3 — Non-Fatal Total Failure Guard**:
   - If both Tier 1 and Tier 2 fail (e.g., corrupt booster or empty test frame), record `metrics["shap_status"] = "failed"`, `metrics["shap_computed"] = False`, and `metrics["shap_error_type"] = type(native_exc).__name__`.
   - Training, model persistence (`joblib.dump`), and JSON metrics generation must still complete cleanly without raising unhandled exceptions.

### 2.3 Mathematical Contract: Raw-Margin / Log-Odds Additivity
For binary logistic classification (`objective="binary:logistic"`), XGBoost's prediction before the sigmoid link function is the log-odds margin:
$$z(x) = \ln\left(\frac{p(x)}{1 - p(x)}\right)$$

Under TreeSHAP efficiency axioms, the feature attributions $\phi_j(x)$ sum with the background base value $\phi_0$ to equal this raw margin exactly:
$$\phi_0 + \sum_{j=1}^{M} \phi_j(x) = z(x)$$

Validation rules:
- Obtain `raw_margin = booster.predict(dtest, output_margin=True, iteration_range=(0, best_iteration + 1))`.
- Assert `np.allclose(contributions[:, :-1].sum(axis=1) + contributions[:, -1], raw_margin, rtol=1e-4, atol=1e-5)`.
- Record `metrics["shap_raw_margin_additivity"] = True` only upon numeric verification.
- Output units are permanently documented as `"raw_margin_log_odds"`.

### 2.4 Visualization Suite: Bar, Beeswarm, and Directional Matplotlib Fallback
The training pipeline shall generate two visual artifacts:
1. **Summary Bar Chart** (`reports/ml/{version}_shap_summary.png`):
   - Horizontal bar plot ranking the top 15 features by mean absolute SHAP value:
     $$\overline{|\phi_j|} = \frac{1}{N} \sum_{i=1}^N |\phi_{i, j}|$$
2. **Beeswarm Distribution Plot** (`reports/ml/{version}_shap_beeswarm.png`):
   - Visualizes feature attribution distributions, variance, and directional impact (e.g., whether high IV increases or decreases win probability).
   - **Dual-Mode Generation**:
     - *Mode A (when `shap` is installed)*: Use `shap.summary_plot(values, X_test, show=False, max_display=15)` or `shap.plots.beeswarm`.
     - *Mode B (Native Fallback when `shap` is absent)*: Implement a pure Matplotlib directional scatter/jitter plot:
       - Rank top 15 features by mean $|\text{SHAP}|$.
       - For each feature $j$, plot points $(\phi_{i, j}, y_j)$ with subtle vertical jitter $\mathcal{U}(-0.15, 0.15)$ to avoid point stacking.
       - Normalize feature values $X_{i, j}$ to $[0, 1]$ (percentile-scaled or min-max) and apply the `coolwarm` colormap (blue = low feature value, red = high feature value).
       - Render a zero vertical reference line ($x=0$) and a colorbar legend indicating feature value magnitude.
   - Both plotting modes must execute headless (`matplotlib.use("Agg")`), enclose file writes in `try/finally plt.close(fig)`, and handle directory creation and stale file cleanup gracefully.

### 2.5 Feature Drift & Rolling-Window Stability Auditing
To detect whether feature importance degrades or shifts across market regimes:
- Function: `audit_shap_stability(df: pd.DataFrame, feature_cols: List[str], ...)`
- **Chronological Windowing**:
  - Split labeled dataset into $W$ rolling chronological windows (default $W=4$ or monthly windows, minimum samples per window $N_{\text{min}} = 30$).
  - If total rows $N < 2 \times N_{\text{min}}$, record `status: "insufficient_data"` and return gracefully without raising.
- **Attribution & Stability Metrics**:
  - Compute mean absolute SHAP vector $\bar{\phi}^{(w)}$ for each valid window.
  - Calculate **Rank Stability** via Spearman rank correlation $\rho_s$ between consecutive windows:
    $$\rho_s = 1 - \frac{6 \sum d_i^2}{M(M^2 - 1)}$$
  - Calculate **Top-5 Turnover Rate**: Proportion of top-5 features entering or leaving the top tier across windows.
  - Calculate **Feature Attribution Drift Score**:
    $$\text{Drift}_j = \frac{|\bar{\phi}_j^{(w)} - \bar{\phi}_j^{(w-1)}|}{\bar{\phi}_j^{(w-1)} + 10^{-6}}$$
- **Drift Classification**:
  - If mean $\rho_s \ge 0.65$ and top-3 features are persistent: `stability_verdict = "stable"`.
  - If mean $\rho_s < 0.50$ or top features experience $> 100\%$ attribution swing: `stability_verdict = "drift_detected"`, with list of `flagged_features`.
- Output is captured in `metrics["shap_stability_audit"]`.

### 2.6 Metadata Enrichment Schema
All training reports (`task183_offline_metrics.json` and `{version}_offline_metrics.json`) will be enriched with a structured `shap_metadata` block:
```json
"shap_metadata": {
  "model_version": "v8",
  "feature_schema_version": "v1.0",
  "feature_count": 42,
  "feature_schema_hash": "e3b0c442",
  "training_window_start": "2026-07-08T09:15:00+00:00",
  "training_window_end": "2026-08-15T15:30:00+00:00",
  "testing_window_start": "2026-08-16T09:15:00+00:00",
  "testing_window_end": "2026-09-11T15:30:00+00:00",
  "sample_size_total": 232,
  "sample_size_train": 185,
  "sample_size_test": 47,
  "shap_backend": "shap_tree_explainer",
  "shap_status": "computed",
  "raw_margin_additivity": true,
  "generated_at": "2026-09-12T23:15:00+00:00"
}
```

---

## 3. Implementation Task Breakdown for Code Generator Agent

The implementation of MANM-156 is assigned to the **Code Generator Agent** with the following concrete file-level instructions:

### Task 1: Update Dependency Manifests
- **Files**: `requirements.txt` and `ml_signal/requirements.txt`
- **Instructions**:
  - Add `shap>=0.47.0,<0.50.0` and `matplotlib>=3.7.0,<4.0.0` to `requirements.txt`.
  - Ensure versions in `requirements.txt` and `ml_signal/requirements.txt` are identical.
  - Document that `shap` is required for offline model training, evaluation, and audit pipelines, while live inference in `predictor.py` remains dependency-free of `shap`.

### Task 2: Refactor SHAP Calculation & Resilient Fallback in `ml_signal/train_offline.py`
- **File**: `ml_signal/train_offline.py`
- **Functions to Modify / Add**:
  1. `_compute_shap(model, X_test, feature_cols, metrics)`:
     - Remove the early return `if exc.name == "shap": return`.
     - When `import shap` fails with `ModuleNotFoundError` or any other import exception, capture `tree_exc = exc` and allow execution to fall directly into the `_native_shap_values` block.
     - Handle all output shapes returned by `shap.TreeExplainer` (lists, 3D arrays for multi-class/binary, and `Explanation` objects) to normalize to shape `(N, M)`.
     - If both `shap` and native fallback fail, record `shap_status = "failed"` and return safely without crashing.
  2. `_native_shap_values(model, X_test, best_iteration)`:
     - Return `(contributions[:, :-1], iteration_range, contributions[:, -1])` (or keep existing signature and verify additivity internally).
     - Verify additivity against `booster.predict(dtest, output_margin=True)`.
  3. `_save_shap_beeswarm_plot(values, X_test, feature_cols, beeswarm_path, backend)`:
     - If `shap` is available and backend is `shap_tree_explainer`, use `shap.summary_plot(values, X_test, show=False)`.
     - If in native fallback (`xgboost_pred_contribs`) or `shap` plot fails, render the pure Matplotlib directional scatter/jitter plot colored by normalized feature values (`coolwarm`).
     - Save to `beeswarm_path` at 150 DPI, wrapping figure disposal in `try/finally plt.close(fig)`.
  4. `audit_shap_stability(df, feature_cols, n_windows=4, min_window_samples=30)`:
     - Implement rolling window chronological splits.
     - Calculate mean absolute SHAP, rank correlation ($\rho_s$), and turnover across windows.
     - Flag feature drift and return stability dictionary.
  5. `_offline_report_paths(repo, version)`:
     - Update to return `(report_path, versioned_report_path, shap_summary_path, shap_beeswarm_path)`.
  6. `run_training(...)`:
     - Assemble `shap_metadata` including training/test timestamp boundaries, sample sizes, and feature schema versioning/hash.
     - Call `audit_shap_stability` and record `"shap_stability_audit"` in metrics.
     - Save both summary bar plot and beeswarm plot.

### Task 3: Invariant Migration & New Unit Test Suite
- **Files**: `tests/unit/test_task183_ml_offline.py` and `tests/unit/test_task156_shap_support.py`
- **Instructions**:
  - Update `test_missing_shap_is_graceful_and_persists_model_and_report` in `tests/unit/test_task183_ml_offline.py`:
    - Previously, missing `shap` expected `shap_computed = False` and `shap_status = "shap_unavailable"`.
    - Under ADR-156, missing `shap` **must fall back to native XGBoost SHAP**: assert `metrics["shap_computed"] is True`, `metrics["shap_backend"] == "xgboost_pred_contribs"`, `metrics["shap_status"] == "computed"`, and `metrics["shap_top_features"]` is populated!
  - Create `tests/unit/test_task156_shap_support.py`:
    - Test Tier 1: Mocked `shap` explainer success with dimension and additivity assertions.
    - Test Tier 2: `ModuleNotFoundError` triggers native `pred_contribs` fallback with exact feature ranking.
    - Test Tier 3: Runtime failure in `TreeExplainer` falls back to native XGBoost.
    - Test Tier 4: Total failure (both Tier 1 and Tier 2 fail) gracefully reports `"failed"` without crashing.
    - Test Tier 5: Raw-margin log-odds additivity validation $\sum \phi_j + \phi_0 = z(x)$.
    - Test Tier 6: Beeswarm plot generation for both external SHAP and pure Matplotlib fallback modes.
    - Test Tier 7: Rolling window drift audit with normal data and sparse data handling.
    - Test Tier 8: Metadata completeness (timestamps, sample sizes, feature schema hash).

---

## 4. Alternatives Considered & Rejected

1. **Rely Exclusively on XGBoost Feature Gain**:
   - *Rejected*: Feature gain calculates the average reduction of split criterion (impurity) across trees. It is purely global, provides no individual sample attribution, cannot explain directional bias (long vs. short), and fails to identify regime shifts.
2. **Mandate `shap` with No Fallback (Fail Hard if `shap` Missing)**:
   - *Rejected*: Violates runtime resilience. A missing C-extension or platform incompatibility in constrained CI/container environments would break offline training entirely.
3. **Use KernelSHAP as Fallback**:
   - *Rejected*: KernelSHAP is model-agnostic but requires sampling $2^M$ subsets, leading to exponential or sampling-heavy $O(M \times N \times \text{samples})$ complexity. TreeSHAP runs in $O(T \times L \times D^2)$ time, where native XGBoost `pred_contribs` executes in milliseconds.
4. **Skip Beeswarm Plot When External `shap` Package is Absent**:
   - *Rejected*: Directional attribution and value spread are critical for audit sign-off. Implementing a lightweight pure Matplotlib directional scatter fallback ensures visual parity in any environment.

---

## 5. Performance, Latency & Security Considerations

- **Inference Latency**: Zero impact. `shap` is never imported in the live prediction path (`ml_signal/predictor.py`), preserving $<1\text{ms}$ live signal evaluation.
- **Training Time**: Native XGBoost `pred_contribs=True` is implemented in optimized C++ within the XGBoost library itself and adds $<50\text{ms}$ overhead on holdout sets ($N \approx 50$ to $500$).
- **Memory Safety**: Headless Matplotlib execution explicitly isolates figures with `plt.close(fig)` inside `finally` blocks, preventing memory leaks during iterative or rolling training runs.
- **Secret & Data Protection**: No credentials or private telemetry are stored in plots or metrics reports. All timestamps are sanitized ISO 8601 strings.

---

## 6. Definition of Done (DoD)

1. Root `requirements.txt` and `ml_signal/requirements.txt` contain compatible `shap` and `matplotlib` specifications.
2. `_compute_shap` seamlessly falls back to `xgboost_pred_contribs` when `shap` is unavailable (`ModuleNotFoundError`).
3. Raw-margin log-odds additivity is mathematically validated and logged (`shap_raw_margin_additivity = True`).
4. Both summary bar charts and directional beeswarm plots are successfully rendered and saved.
5. Rolling window SHAP stability audit calculates rank correlations and flags drift, gracefully handling sparse windows.
6. Offline metrics JSON files contain complete `shap_metadata` blocks.
7. 100% test pass rate across `tests/unit/test_task183_ml_offline.py` and `tests/unit/test_task156_shap_support.py`.
8. Architecture and documentation synced to Obsidian and repo docs.
