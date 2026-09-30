# MANM-158 Diagnose and fix setup-level weaknesses
**Date:** 2026-09-12
**Status:** OI wall RCA approved and rule implemented; out-of-sample validation pending; failed breakout deferred

## Goal
For this phase, validate the recently changed OI-wall retest rule against the last recorded OI-wall trade, identify a concrete correction to false retest qualification, and define validation without fitting to that one trade. Return to `FAILED_BREAKOUT` in a later phase.

## Inputs
- Issue description from MANM-158
- Codebase: `detectors/oi_wall.py`, `detectors/oi_wall_entry.py`, `detectors/breakout.py`, `engine.py`, and configuration profiles
- Read-only `trade_analytics`, `ares_signals`, and `ml_collection` queries in the Trading signal data Supabase project

## Tools / Scripts to Use
- Read-only filter replay of September 28 candles against the PR #103/#107 and PR #119 rules, checked against persisted OI-wall telemetry.
- Full-engine out-of-sample replay only after complete timestamped candles, option chains, structural levels, and code/config versions are available. The current historical data cannot reproduce changed detector decisions.

## Expected Output
- ADR detailing the September 28 OI-wall root cause and proposed retest rule
- Code change and symmetric regression tests after ADR approval
- Out-of-sample validation when complete historical or prospective data exist

## Acceptance Criteria
- Explain why the September 28 signal passed the new OI-wall state machine and whether it showed a real price rejection.
- Preserve immediate valid-retest entries while rejecting contrary-direction retest candles.
- Assess stop geometry separately; do not disable the setup or widen stops on this evidence alone.
- Validate the adjustment using out-of-sample replay when the necessary inputs are available; label filter-only replay honestly.
- No curve-fitting exclusively to the historical sample.

## Edge Cases
- PR #107 is MANM-110's tracked-wall priority fix; PR #119 introduced immediate retest qualification. The ticket's MANM-148 attribution was incorrect.
- Preserve wall state, acknowledgement, and telemetry when a retest candle fails directional confirmation.
- Assess OI freshness and market regime without interpreting OI growth alone as support defense.
- Failed-breakout score and behavior are outside this OI-wall phase.
- Treat the existing July–September time slices as exploratory, because the split was specified after inspecting their trades.
