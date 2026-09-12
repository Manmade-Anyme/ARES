# MANM-159 Investigate and resolve directional performance asymmetry (Bullish vs Bearish)
**Date:** 2026-09-12
**Status:** draft

## Goal
Identify the structural causes behind the severe underperformance of bullish trades compared to bearish trades, and implement a remediation strategy (e.g., asymmetrical risk/reward targets, regime-aware gating, or revised trend confirmation rules).

## Inputs
- Issue MANM-159 details: 
  - Bearish trades: 127 trades, +446.90 points, 33.1% win rate (+3.52 pts/trade).
  - Bullish trades: 106 trades, -176.05 points, 31.1% win rate (-1.66 pts/trade).
- Existing trade logs and execution logic in `execution/` and `ml_signal/`.
- Position management logic in `position_manager.py` and `engine.py`.

## Tools / Scripts to Use
- Python scripts for data analysis (e.g., bootstrap confidence interval analysis, winsorized/trimmed returns).
- AI grep / search to locate relevant trading rules.
- Architect Agent to write ADR and design the solution.
- Code Generator Agent to implement the remediation.

## Expected Output
1. ADR explaining the chosen remediation for the directional asymmetry.
2. Updated trading rules / execution code implementing the fix.
3. Analysis report confirming the root cause.

## Acceptance Criteria
- Statistical analysis (bootstrap confidence intervals, outlier sensitivity checks) is conducted and documented.
- Structural causes of bullish underperformance are identified.
- Proposed remediation is implemented and tested.
- ADR is written and saved in `directives/adr/`.

## Edge Cases
- Ensure the remediation does not negatively impact the currently profitable bearish strategy.
- Account for potential false positives in bullish signals due to macro regime shifts.
