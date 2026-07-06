# TASK-177 Directive — Trend-Continuation Detector

**Status:** Proposed — do not implement until the user approves the ADR.
**ADR:** `directives/adr/TASK-177_trend-continuation-detector.md`
**Mode:** Design-First (new detector, not a tweak)

## Objective

Give ARES a trend-aligned setup so trending sessions produce tradeable flow. Ship it observation-only first, validated by Dhan 1-min replay, then flip live via a separate config PR.

## Why now

03→06 Jul: 3 sessions of steady up-move, zero valid setups. All three existing detectors are fade detectors; the gate stack (correctly, per Dhan-verified outcome analysis 2026-07-06) blocks their counter-trend output. Missing capability, not mis-tuning — see ADR "Alternatives Considered".

## Scope

**In:**
1. `detectors/continuation.py` — `TrendContinuationDetector` with IDLE→ARMED→PULLBACK→SIGNAL state machine (regime persistence → VWAP/level pullback → resumption trigger). Full spec in ADR.
2. `models.py` — `SetupType.TREND_CONTINUATION`.
3. `config_profiles.py` — 7 new `continuation_*` tunables, both profiles (expiry disabled).
4. `engine.py` — wire into priority chain: breakout → oi_wall → **continuation** → exhaustion; alert-only handling identical to exhaustion's Filter D pattern (reason string: `"Observation only — continuation entries gated by config (continuation_alert_only)"`).
5. Alerts — reuse the gray OBSERVATION ONLY card styling from TASK-175.
6. Tests — TDD-first; state transitions, scoring matrix, SL = pullback extreme (no buffer), structural target selection reuse, filter interplay, profile switching, alert-only flag.
7. Dhan-replay validation of last ~10 sessions recorded in the build log before merge (tooling: session scratchpad scripts from 2026-07-06 analysis; Dhan creds via Supabase `api_keys` table, provider=DHAN, per `storage.py:255`; throttle ~4s between `intraday_minute_data` calls — DH-904).

**Out:**
- HIGH-exhaustion re-enable / trend-filter exemption (separate decision, separate task).
- Phase-2 live enablement (separate config-flip PR after observation criteria met).
- detector_scores collector fix (separate open task; should land first or alongside).
- Expiry-day enablement (`continuation_enabled=True` on `EXPIRY_CONFIG`) — deferred to a follow-up TODO; expiry's faster candle cadence needs its own `continuation_*` tuning, which can't be derived without observation data first.

## Acceptance criteria

- All new logic behind `continuation_enabled` / `continuation_alert_only`; default state emits observation alerts only, never trades, never consumes cooldown.
- Emitted signals are trend-aligned by construction (Filter E rule reused) — assert in tests that Filter E never downgrades a continuation signal.
- Existing 231 tests green; new tests cover every state transition and both profiles.
- Build log + Obsidian synced; ADR INDEX row flipped to Approved on merge.

## Workflow

Branch `feature/TASK-177-trend-continuation` → TDD → PR → halt for user review/merge. No direct pushes to main.
