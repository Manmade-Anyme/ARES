# Problem Brief — TASK-20261108
**Date:** 2026-11-08
**Status:** approved

## Problem Statement (Original)
Fix `IVBreakoutDetector` State Buffer Continuity Under Engine Cooldown & Priority Short-Circuiting.
In `engine.py`, `IVBreakoutDetector` does not receive candle or IV updates whenever the engine is in a signal cooldown or when a higher-priority detector branch evaluates. This creates gaps in its rolling buffers, leading to distorted breakout base calculations, inaccurate VWAP Z-scores, and stale IV velocity baselines once cooldown expires.

## Problem Statement (Simplified)
`IVBreakoutDetector` needs uninterrupted observation of every session candle and ATM option IV to maintain an accurate rolling consolidation base (25 bars), VWAP standard deviation, and IV velocity baseline. Gaps caused by engine cooldown or prior detector evaluation short-circuiting corrupt this rolling state.

## What We Know
- `IVBreakoutDetector.observe()` was specifically created to record candle and IV state across every tick regardless of cooldown or priority.
- Step 1 buffer updates in `AresEngine.tick()` at lines 176–181 invoke `self.iv_breakout_detector.observe(...)` before cooldown evaluation and priority dispatch.
- `observe()` contains an idempotency check preventing duplicate candle appends if both `observe()` and `update()` evaluate on the same candle.
- Cooldown bypasses steps 5 (detectors priority chain), but Step 1 runs unconditionally.
- Higher-priority detectors (`BreakoutDetector`, `OIWallDetector`, `TrendExpansionDetector`, `TrendContinuationDetector`) can short-circuit before `IVBreakoutDetector.update()` is called, meaning `IVBreakoutDetector` relies on Step 1 `observe()` for continuity.

## What We Don't Know (Assumptions to Validate)
- Whether `isinstance(candle, OHLCVCandle)` check in Step 1 observation is strictly required to guard against malformed/mock candle feeds without breaking existing mock tests.
- Whether duplicate candle protection in `observe()` should strictly check candle timestamp equality in addition to object identity `is not candle`.

## Edge Cases Identified
- **Session boundary transitions:** Session date change triggers `reset_session()` inside `observe()`, properly clearing history across trading days.
- **Cooldown duration spanning multiple candles:** During a 10–15 minute cooldown, 10–15 candles must be continuously captured by `observe()`.
- **Higher-priority setup triggered:** A breakout or OI wall signal fires on candle N, causing `iv_breakout_detector.update()` to be skipped. Candle N must still exist in `iv_breakout_detector.candle_history` on candle N+1.
- **Missing or non-positive IV:** IV history must only record valid, positive IV readings; missing or zero IV must not pollute the velocity baseline.
- **Defensive mock/non-OHLCVCandle:** Non-OHLCVCandle inputs should be cleanly guarded without throwing uncaught exceptions.

## Recommended Next Step
1. Add `isinstance(candle, OHLCVCandle)` guard in `AresEngine.tick()` Step 1 buffer update and strengthen idempotency in `IVBreakoutDetector.observe()`.
2. Add comprehensive unit tests in `tests/unit/test_iv_breakout_detector.py` validating continuous buffer accumulation across multi-candle cooldowns and priority short-circuits.
3. Verify test coverage and sync docs.
