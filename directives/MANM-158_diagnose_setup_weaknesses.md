# MANM-158 Diagnose and fix setup-level weaknesses
**Date:** 2026-09-12
**Status:** ready

## Goal
Identify the root causes for the negative expectancy in the `OI_WALL_REJECTION` and `FAILED_BREAKOUT` setups, and propose/validate adjustments to risk controls or parameters without overfitting.

## Inputs
- Issue description from MANM-158
- Codebase: `detectors/oi_wall.py` and `detectors/breakout.py`
- Historical performance data provided in the issue

## Tools / Scripts to Use
- Out-of-sample replay across historical data for validation

## Expected Output
- ADR detailing the root-cause analysis and proposed adjustments
- Code changes applying the adjustments (if applicable)
- Validation results from historical replay

## Acceptance Criteria
- Root-cause analysis explaining negative expectancy in `OI_WALL_REJECTION` and `FAILED_BREAKOUT`.
- Assessment of whether tighter risk controls, structural updates, or disabling the setups are necessary.
- Validation of adjustments using out-of-sample replay.
- No curve-fitting exclusively to the historical sample.

## Edge Cases
- Interaction with recent fixes like MANM-148 / PR #107 (retest priority).
- Proper application of market regime filtering.
