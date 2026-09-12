# MANM-157 Improve Confidence Calibration and Reliability Across Signal Tiers
**Date:** 2026-09-12
**Status:** in_review

## Goal
Diagnose and eliminate inverse calibration across signal confidence tiers (HIGH vs MEDIUM), establish mathematically sound calibration evaluation metrics (Reliability diagrams, ECE, Brier score decomposition), stratify performance across market dimensions, and enforce a strict out-of-sample statistical significance gate before assigning HIGH confidence.

## Summary of Architecture & Design
- **Audit Findings & Root Cause**:
  - Audit of 233 historical trades revealed inverse calibration: HIGH confidence signals delivered 31.6% win rate (12/38) and +50.00 pts (+1.32 pts/trade), whereas MEDIUM confidence signals delivered 32.3% win rate (63/195) and +220.85 pts (+1.13 pts/trade).
  - Statistical significance testing (Fisher exact test $p = 1.0000$, Chi-square test $p = 1.0000$) demonstrated that HIGH and MEDIUM confidence tiers currently separate zero distinct outcome distributions.
  - Root cause traced to arbitrary heuristic thresholding (`score / max_score >= 0.6`) in `models.py` (`confidence_from_score`), uncalibrated probability cutoffs in ML inference (`high_threshold = 0.70` on models with test AUC $< 0.50$), and absence of production degradation gating.
- **Architect ADR-157 Authored (`directives/adr/MANM-157_improve-confidence-calibration.md`)**:
  - **Calibration Metrics Framework (`ml_signal/calibration.py`)**:
    - Discretized reliability diagram binning (uniform and quantile intervals).
    - Expected Calibration Error (ECE) and Maximum Calibration Error (MCE).
    - Brier score decomposition (Murphy 1973): $BS = \text{REL} - \text{RES} + \text{UNC}$.
    - Statistical hypothesis testing suite: one-sided Fisher exact test for win rate separation, and one-sided Mann-Whitney U test for PnL expectancy separation.
  - **Stratified Calibration Evaluator (`ml_signal/stratified_evaluator.py`)**:
    - Stratifies realized trade performance across 4 operational axes: Setup Type (`FAILED_BREAKOUT`, `OI_WALL_REJECTION`, `EXHAUSTION_REVERSAL`, `TREND_CONTINUATION`), Direction (`BULLISH` vs `BEARISH`), Volatility Regime (Low IV vs High IV), and Time of Day (09:15-10:30, 10:30-13:30, 13:30-15:30 IST).
    - Automatically exports structured JSON reports to `reports/ml/confidence_calibration_stratified_report.json`.
  - **Strict Out-of-Sample Policy Gate (`ml_signal/calibration_policy.py`)**:
    - Enforces that no signal may be designated `HIGH` confidence unless historical out-of-sample validation proves statistically significant superiority ($p < 0.05$ on both win rate and expectancy with $N \ge 30$).
    - Unvalidated or inverted `HIGH` tiers are automatically suppressed to `MEDIUM` at signal emission, with explicit audit reasons logged in `reasons` and `market_context`.
  - **Post-Hoc Probability Calibrator (`ml_signal/calibrator.py`)**:
    - Fits Platt scaling / Isotonic regression on out-of-fold validation splits to ensure monotonic mapping from predicted probability to realized outcome frequency.
- **Assigned Implementation Tasks**:
  - Authored comprehensive file-level contracts and assigned execution to Code Generator Agent, awaiting human ADR approval.
