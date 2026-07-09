# TASK-184 — Restore MEDIUM-confidence failed-breakout signals

**Date:** 2026-07-09
**Status:** complete (PR pending user merge)
**Type:** deliberate product decision / config revert

> ⚠️ **FOR FUTURE AGENTS — THIS IS INTENTIONAL, NOT A BUG.**
> `breakout_failure_min_score = 2` is a deliberate choice. Do **not** "harden",
> "tighten precision", or revert it to 3. If a review flags it, read this file.

## Goal
Stop the failed-breakout detector from silently swallowing MEDIUM-confidence
setups. A MEDIUM breakout must reach the user as a live signal (already labeled
`confidence = "MEDIUM"`), not be pre-filtered by the engine.

## Why (product rationale)
ARES is **not** a pure autotrader — the user actively manages their own trades
and uses ARES signals as **directional confirmation**:
- a signal **in the direction** of an open position → adds confidence to hold;
- a signal **against** it → a prompt to reconsider / trim.

A silent detector provides neither. Signal *presence* has value independent of
whether that specific setup would have been individually profitable. The
`confidence` field ("MEDIUM"/"HIGH") already lets the user weight it — the engine
should surface the information, not decide for them to hide it. This is the same
"no withholding gates" principle behind TASK-182 (which removed the observation /
speed / IV-crush suppressors).

## What changed
- `config_profiles.py`: `TuningConfig.breakout_failure_min_score` class default
  **3 → 2**. Both `NON_EXPIRY_CONFIG` and `EXPIRY_CONFIG` inherit the default, so
  both profiles now fire the MEDIUM tier. No other knob touched.

## What did NOT change (guard rails still in place)
- `closed_back` is still a **mandatory hard gate** (a breakout must actually close
  back across the level). It is not a scored condition (TASK-172 item 8 stands).
- The 4-condition score matrix is unchanged (weak_volume, iv_falling,
  writers_active, deep_close; TASK-174). Score **0-1 still never fires** — pure
  noise is still filtered.
- The R:R gate (`min_rr_ratio`) is unchanged — degenerate risk:reward setups are
  still rejected.
- Confidence banding is unchanged: score 2/4 = MEDIUM, 3/4 = HIGH
  (`models.confidence_from_score`, ≥60% = HIGH).

## Evidence that motivated it
Audit of `ml_collection` (2026-06-29 → 07-09) + a live Dhan pull:
- The three "fader" detectors (FailedBreakout / OIWall / Exhaustion) went silent
  07-07 → 07-09 while only TrendContinuation fired.
- Breakout silence traced to this gate: on 07-07 there were **10 genuine
  closed-back failed-breakout candidates; 3 reached exactly score 2** and were
  all filtered by `min_score = 3`. The commonly-missing 3rd point is
  `weak_volume`, which is anti-correlated with a real breakout (breakout candles
  usually have *high* volume).
- The `9ddb0cf` (2026-06-24) "working" baseline used `min_score = 2`.

## Verification
- `tests/unit/test_task184_medium_breakout.py` (new, 4 tests): a score-2 breakout
  fires and is labeled MEDIUM; score-1 still returns None; both profiles report
  min_score == 2.
- Updated score-mechanic tests in `test_task174_oi_scoring.py`,
  `test_task175_sl_config_obs.py`, `test_audit_p1_tuning.py` to assert the
  scoring effect via confidence / reason strings (a dropped point → MEDIUM +
  absent reason) instead of the old fire-vs-None, which encoded min_score = 3.
- **250 tests green.**
- End-to-end replay: the real `FailedBreakoutDetector` with the new config fires
  **4 MEDIUM breakouts on 07-07** (min_score=3 baseline: 0).

## Honest caveat (not a blocker — recorded for context)
On 07-07 a forward P&L sim of the recovered fader signals was net **negative**
(counter-trend fades in a down-drift; ~-47 pts), and the live system's
continuation trades that window also lost. Restoring MEDIUM increases signal
*count*, not guaranteed profit. That is acceptable **by design**: the value here
is the confirmation signal for manual trade management, not the standalone
auto-P&L of each fade. A future task may add a trend-alignment guard so faders
only fire into a level that holds — tracked separately, out of scope for TASK-184.
