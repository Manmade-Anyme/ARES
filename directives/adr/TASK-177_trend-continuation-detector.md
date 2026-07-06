# Architecture Decision Record — TASK-177 (Trend-Continuation Detector)

**Date:** 2026-07-06
**Status:** Proposed (awaiting user approval before implementation)

## Problem Statement

All three ARES detectors (Failed Breakout, OI Wall Rejection, Exhaustion Reversal) are *fade* detectors — they trade against the prevailing move. In a trending market they can only produce counter-trend candidates, which the TASK-171→175 gate stack correctly blocks. Result: during the 03-Jul → 06-Jul grind-up, ARES produced 10 signals of which 1 was tradeable; 8 were MEDIUM exhaustion observations that went 0/8 to T1 (Dhan-verified, see `docs/` obs-signal outcome analysis / Obsidian note 2026-07-06).

The system has no way to participate in a trend. Loosening any existing gate was evaluated against broker data and only re-admits historically losing flow (pre-gate era: -159.6 pts / 24 trades Dhan-verified). The gap is a missing capability, not mis-tuning.

## Decision

Add a fourth detector: **`TrendContinuationDetector`** (`detectors/continuation.py`, `SetupType.TREND_CONTINUATION`) that enters *with* the regime on pullback-and-resume structure.

### Core logic (state machine, mirrors `BreakoutState` pattern)

```
IDLE ──regime persists N candles──▶ ARMED ──pullback to VWAP band/level──▶ PULLBACK ──resumption candle──▶ SIGNAL → reset
                                      │                                        │
                                      └──── regime breaks ── reset ◀── pullback window expires ────┘
```

1. **Regime**: identical rule to engine Filter E (uptrend = `close > vwap AND close > pdl`; downtrend = `close < vwap AND close < pdh`), required to hold for `continuation_regime_min_candles` consecutive candles before the detector arms. Reusing the Filter E rule guarantees emitted signals are trend-aligned by construction and pass Filter E untouched.
2. **Pullback**: price retraces from the session extreme to within `continuation_pullback_vwap_pts` of VWAP (or touches a structural level from `levels`), without breaking the regime. Pullback must complete within `continuation_pullback_max_candles` or the state resets.
3. **Resumption trigger**: first candle after the pullback that closes in the trend direction (bull: `close > open` and `close > vwap`).

### Scoring matrix (4 conditions, shared `confidence_from_score`)

| # | Condition | Meaning |
|---|---|---|
| 1 | Shallow pullback | Retrace held above VWAP (bull) — buyers defended the mean |
| 2 | Resumption volume | Trigger-candle volume ≥ `continuation_resume_volume_ratio` × rolling average |
| 3 | Strong regime | Regime persisted ≥ 2× the arming minimum |
| 4 | Room to run | Distance to the next opposing structural level ≥ `structural_target_min_distance_pts` |

Minimum score to emit: `continuation_min_score` (default 2). Confidence via the shared ≥60% HIGH bar (3+/4 = HIGH), consistent with TASK-172.

### Trade construction

- Entry = resumption close; entry zone via existing `entry_zone_offset_pts`.
- **SL = pullback swing extreme, exact, no buffer** (TASK-175 convention).
- Targets via the shared structural target selection (`structural_target_min_distance_pts`, `target_1_fallback_min_pts`, `target_2_fallback_min_pts`), falling back to `target_1_pts`/`target_2_pts`.

### Engine integration

- Priority order: Failed Breakout → OI Wall → **Trend Continuation** → Exhaustion. Reversal-at-structure setups keep precedence (higher precision); continuation outranks exhaustion, which is observation-gated anyway.
- All existing filters apply unchanged: speed filter (A), R:R gate (C), IV-crush (B, MEDIUM only), trend filter (E — passes by construction), shared cooldown, tick exits (TASK-173), multi-day carry (TASK-170).

### Rollout: observation-first

Phase 1 ships with **`continuation_alert_only = True`** — the same validation path used for exhaustion. Alerts + DB logging, no trades, no cooldown consumption. Additionally, replay the detector against Dhan 1-min history (fetch tooling and creds path established 2026-07-06) for the last ~10 sessions before the PR merges, and report simulated outcomes under the user's trading model in the build log.

Phase 2 (separate config-flip PR): enable live after ≥10 observed signals with T1-reach materially above the 14-21% historical baseline. Expiry profile stays disabled (`continuation_enabled = False`) until non-expiry behavior is proven — expiry moves die too fast for untested pullback logic.

### New TuningConfig fields

| Field | Non-expiry | Expiry | Notes |
|---|---|---|---|
| `continuation_enabled` | True | **False** | Master switch per profile |
| `continuation_alert_only` | True | True | Phase-1 observation mode |
| `continuation_regime_min_candles` | 15 | 10 | Arming persistence |
| `continuation_pullback_vwap_pts` | 10.0 | 8.0 | VWAP proximity band |
| `continuation_pullback_max_candles` | 10 | 6 | Pullback window before reset |
| `continuation_resume_volume_ratio` | 1.2 | 1.3 | Scored condition 2 |
| `continuation_min_score` | 2 | 3 | Emission bar |

Defaults are first-guess values — the observation phase exists precisely to tune them; the detector_scores collector fix (open follow-up task) should land first or alongside so near-misses are measurable from day 1.

## Alternatives Considered

1. **Loosen existing gates** — rejected. Dhan-verified per-gate analysis (2026-07-06): every loosening candidate only re-admits losing flow; none would have produced a valid setup during the 3-day uptrend.
2. **Re-enable HIGH-only exhaustion with trend-filter exemption** — orthogonal, still open. Captures reversal days, not trend days; n=3 evidence; explicitly NOT bundled into this task.
3. **VWAP-cross momentum entry (no pullback)** — rejected: enters mid-move with no structural SL reference; violates the buffer-less exact-SL convention and produces poor R:R at the Filter C gate.

## Risks

- **Strong trends may never pull back to VWAP** → structural-level touch also qualifies as a pullback; observation phase will show trigger frequency.
- **Chop masquerading as regime** → regime persistence requirement + existing speed filter on MEDIUM.
- **Score inflation** (4-condition matrices have coarse granularity) → same known trade-off accepted in TASK-174; observation data will calibrate `continuation_min_score`.

## Definition of Done

- `detectors/continuation.py` + `SetupType.TREND_CONTINUATION` + engine wiring, TDD-first: state machine transitions, scoring, SL/target construction, filter interplay, both profiles, alert-only rollout flag.
- All existing tests remain green (231 as of TASK-175).
- Dhan-replay validation results recorded in the build log before merge.
- Docs sync: build log + Obsidian, ADR INDEX updated.
- Branch `feature/TASK-177-trend-continuation`, PR — no direct pushes to main; user merges.
