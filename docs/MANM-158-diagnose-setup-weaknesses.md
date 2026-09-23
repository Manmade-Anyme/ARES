# MANM-158 Diagnose and Fix Setup-Level Weaknesses: OI Wall Rejection and Failed Breakout
**Date:** 2026-09-12
**Status:** in_review

## Goal
Perform root-cause diagnostics on setup-level performance weaknesses (`OI_WALL_REJECTION` and `FAILED_BREAKOUT`), identify structural mechanisms causing negative expectancy, evaluate risk controls, and establish an architectural remediation plan and out-of-sample validation framework.

## Summary of Architecture & Design
- **Empirical Audit Findings**:
  - Across 233 historical trades (audit baseline 2026-09-11), `TREND_CONTINUATION` (+372.70 pts, 34.4% WR) and `EXHAUSTION_REVERSAL` (+92.90 pts, 30.2% WR) drive all system profitability.
  - `FAILED_BREAKOUT` (17 trades, -51.55 pts, 35.3% WR, -3.03 pts/trade) and `OI_WALL_REJECTION` (20 trades, -143.20 pts, 30.0% WR, -7.16 pts/trade) accumulated -194.75 points of negative PnL drag.
  - Without these two setups, net system spot PnL would be +465.60 pts (+71.9% higher).
- **Five Identified Root Causes**:
  1. *Decoupled Entry vs Fixed Stop Geometry (Stranded Stop)*: In `apply_per_type_levels()`, SL is placed a fixed distance from the entry candle close. Because re-test confirmation closes 15–25 pts away from the wall strike or breakout level, placing a 16 pt or 12 pt stop puts the stop loss *in front of* the structural barrier. Price noise triggers premature fake stop-outs before the barrier is even challenged.
  2. *Counter-Trend Fading in Trend Regimes*: Stripping trend-regime filters in TASK-182 left these mean-reverting fade setups exposed to institutional trend days, where option writers get steamrolled.
  3. *Static vs Dynamic Open Interest (Zombie Walls)*: The 4M contracts threshold qualifies stale multi-week positions even when institutional writers are actively covering or migrating strikes.
  4. *Over-Sensitive Trigger in Failed Breakout*: Reducing `breakout_failure_min_score` to 2 in TASK-184 caused routine shallow pullbacks to trigger counter-trend trades.
  5. *Mathematical Expectancy Deficit*: At 30% win rate and 0.84 win/loss ratio, `OI_WALL_REJECTION` is mathematically guaranteed to generate drawdowns.
- **Architect ADR-158 Authored (`directives/adr/MANM-158_diagnose_setup_weaknesses.md`)**:
  - *Decision 1 (Phase 1 Protection)*: Immediately disable `OI_WALL_REJECTION` live signal emission (`oi_wall_trading_enabled: bool = False`) while preserving background tracking and telemetry logging. Revert `FAILED_BREAKOUT` min score from 2 to 3 and add intraday VWAP regime filtering.
  - *Decision 2 (Phase 2 Structural SL)*: Reform `apply_per_type_levels()` to anchor stop loss to structural levels ($\text{Level} \pm \text{buffer}$) rather than blind fixed point offsets from entry.
  - *Decision 3 (Out-of-Sample Replay Framework)*: Implement `scripts/replay_validation.py` for walk-forward validation across chronological splits (June 24–July 20, July 20–August 15, August 15–September 11) to prevent in-sample curve fitting.
  - *Decision 4 (Promotion Gate)*: Require $N \ge 25$, Win Rate $\ge 40\%$, Expectancy $\ge +2.5$ pts/trade before promoting any gated setup back to live execution.
- **Assigned Implementation Tasks**:
  - Detailed component boundaries, file assignments, and API contracts defined for Code Generator Agent, awaiting human ADR approval.
