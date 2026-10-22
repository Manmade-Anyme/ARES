# TASK-20261022 — Dhan Tick Subscription Fix Verification

Scope: one production-code line, one updated regression expectation, and changelog entry. Publication branch: `codex/task-20261022-dhan-websocket-ticks`, based on merged main `de2ed81`. Verification below used live market data with database writes and Discord sends captured locally; no orders or production restart occurred. Deployment remains subject to human review and merge.

## Change

`fetchers/tick_feed.py:44` now uses `str(settings.security_id)` instead of `int(...)`. The installed SDK passes this through to Dhan's required string `InstrumentList.SecurityId`. No detector thresholds, signal filters, option sizing, ATR rules, or feed health logic changed.

## Test evidence

- Updated subscription-contract regression failed before the fix: `(0, '13', 15)` was absent from numeric subscriptions. 1 failed, 13 passed.
- After the fix: **157 tests passed**, covering tick handling, main/completed candles, manual signals, REST fallback, positions and ATR. TickFeed: **100% line and branch coverage** (44 statements, 4 branches). Eight existing Supabase deprecation warnings.
- Actual patched local `main.run`, without subscription overrides, ran **2026-10-22T12:14:45.653858+05:30 → 2026-10-22T12:17:03.671834+05:30** and stopped normally after three live completed-candle evaluations.
- Source candles: 2026-10-22T12:14:00, 2026-10-22T12:15:00, 2026-10-22T12:16:00; each normalized chain contained 261 rows; OI previous values populated after the initial baseline.
- All four detectors plus the OI entry filter advanced three times. Three real ML feature snapshots were constructed; all database writes were captured locally.
- Real WebSocket LTP reached the application: final sampled price **₹22603.85**. A separate actual-wrapper probe also received 2 ticks (sample ₹22609.85).
- All 19 audited Dhan REST responses returned HTTP 200; zero unhandled async errors and zero paper order records.
- `git diff --check` passed.

## Limits

No natural signal fired during the three local cycles. Actual Discord delivery and a live signal reaching T1/ATR exit remain unobserved; existing runtime regressions passed. The local engine cold-started with three bars, rather than replaying the session. The separate immediate-stop wrapper probe did not complete SDK close within its three-second observation window; the actual three-cycle main-loop run completed shutdown successfully. Existing model compatibility and normal WebSocket close-code warnings remain.

## Evidence

Local files under `/tmp/ares-live-audit-20261022/`: `tick-regression-before.log`, `tick-fix-regressions.log`, `fixed-live-main.log`, `fixed-main-result.json`, `ws-patched-result.json`. The live harness intercepted database mutations and Discord sends and allowed Dhan data endpoints only.

## Publication gate

Independent PR review found no actionable issues after checking the SDK subscription serializer and downstream consumers. Independent QA verified the failing-before/passing-after regression, 100% TickFeed coverage and captured live evidence; a fresh 14-test TickFeed run passed. No baseline JSON exists, and remote CI is reported separately on the pull request.
