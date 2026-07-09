# TASK-185 — Per-Setup-Type SL/Target Config — POC (for approval)

**Status:** PROPOSED — awaiting sign-off before any coding/PR.
**Date:** 2026-07-09
**Scope:** Give each detector type its own SL / T1 / T2, editable from config, applied by one central engine step. Re-tuned monthly by editing config numbers only (no code change).

---

## 1. Decisions locked in the grill

| # | Question | Decision |
|---|----------|----------|
| 1 | Apply semantics | **SL + T1 = fixed absolute per-type (REPLACE structural). T2 = structural S/R**, per-type fallback only when no level exists. |
| 2 | Placement | **Central engine step**, config-driven; runs after the detector builds the signal, before the R:R gate. Detectors are NOT edited. |
| 3 | Config shape | **`per_type_levels` field on `TuningConfig`**, set inline in each profile (same "full instance" style as the rest of the file). Same numbers in EXPIRY and NON_EXPIRY for now; expiry can diverge later. |

Honesty note carried from the grill: the validated mean-pts (below) used a **fixed** T2. Keeping T2 structural means the 40% T1-booking leg is exactly as validated; the 60% runner leg rides to a real level / EOD instead of the fixed cap — usually *better*, but not identical to the grid numbers.

---

## 2. What exists today (verified in code)

- **Stops:** purely structural, no config knob.
  - exhaustion `stop_loss = candle.high/low` (~0.6–4 pt — too tight)
  - continuation `stop_loss = pullback_extreme`
  - oi_wall `stop_loss = strike` (~45–51 pt — too wide, biggest bleed)
  - breakout `stop_loss = level` (~30 pt)
- **Targets:** structural-first, then global constant fallback. Each detector tries real S/R beyond `structural_target_min_distance_pts` (20), else falls back to `settings.target_1_pts` (35) / `target_2_pts` (70).
- **Engine gate:** only an R:R gate remains (`reward/risk >= min_rr_ratio`, `min_rr_ratio = 1.0`). `models.py` `SignalType` + `Direction` enums; `levels` is in scope at the gate; signal carries `trigger_price / stop_loss / target_1 / target_2 / setup_type / direction`.

The 4 setups have **opposite** stop problems — exhaustion too tight (needs widening), oi_wall/breakout too wide (need tightening). A single global floor can only widen, so it structurally cannot fix oi_wall. That's why per-type fixed distances are the right lever.

---

## 3. Proposed values (validated on P&L, replace-semantics)

| setup | SL | T1 | T2 (fallback) | mean pts | win | vs structural |
|-------|----|----|----|----------|-----|---------------|
| EXHAUSTION_REVERSAL | 12 | 24 | 40 | **+8.35** | .54 | +2.23 → +8.35 |
| TREND_CONTINUATION  | 25 | 40 | 80 | **+10.59** | .60 | +4.39 → +10.6 |
| OI_WALL_REJECTION   | 12 | 25 | 40 | **−0.50** | .25 | −25.6 → −0.5 |
| FAILED_BREAKOUT     | 15 | 30 | 55 | **+15.0** | .50 | −6.4 → +15 |

**R:R gate check (T1/SL, must be ≥ 1.0):** EXH 2.0 · CONT 1.6 · OI 2.08 · FB 2.0 → **all pass, nothing suppressed.** Consistent with the no-withholding-gates rule.

**Confidence:** only EXHAUSTION is well-supported (n=13). CONTINUATION n=5, OI_WALL n=4, FAILED_BREAKOUT n=2 — **indicative only**, which is exactly why the monthly re-tune exists. Values ship as config defaults, not truths.

---

## 4. The config change (`config_profiles.py`)

Add a small typed row + a field on `TuningConfig`, set inline in each profile:

```python
@dataclass(frozen=True)
class SetupLevels:
    """Per-setup-type SL / T1 / T2-fallback in absolute points from entry."""
    stop_pts: float
    target_1_pts: float
    target_2_fallback_pts: float   # used ONLY when no structural T2 exists beyond T1

# on TuningConfig:
    per_type_levels: dict[str, SetupLevels] = field(default_factory=dict)
```

In **each** profile (NON_EXPIRY and EXPIRY — identical for now):

```python
    per_type_levels={
        #                        SL   T1   T2fb
        "EXHAUSTION_REVERSAL": SetupLevels(12, 24, 40),
        "TREND_CONTINUATION":  SetupLevels(25, 40, 80),
        "OI_WALL_REJECTION":   SetupLevels(12, 25, 40),
        "FAILED_BREAKOUT":     SetupLevels(15, 30, 55),
    },
```

Monthly re-tune = edit these 12 numbers. Nothing else moves.

---

## 5. The engine change (`engine.py`)

One helper, called on `signal` right **before** the R:R gate (so the gate sees final levels):

```python
def _apply_per_type_levels(signal, settings):
    lv = settings.per_type_levels.get(signal.setup_type.value)
    if lv is None:                      # safety: unknown type -> leave structural
        return
    entry = signal.trigger_price
    sign = 1 if signal.direction == Direction.BULLISH else -1
    signal.stop_loss = entry - sign * lv.stop_pts       # REPLACE (fixed per-type)
    signal.target_1  = entry + sign * lv.target_1_pts   # REPLACE (fixed per-type)
    # T2: keep the detector's STRUCTURAL target_2 if it lies beyond the new T1;
    #     otherwise fall back to the per-type distance.
    beyond_t1 = (signal.target_2 - signal.target_1) * sign > 0
    if not beyond_t1:
        signal.target_2 = entry + sign * lv.target_2_fallback_pts
```

```python
if signal:
    _apply_per_type_levels(signal, settings)   # NEW
    # existing R:R gate follows, now evaluating the per-type levels
```

Why this is low-risk:
- Detectors untouched — they still do all detection + structural T2 discovery.
- T2 stays structural whenever the detector found a real level beyond the new T1; per-type number is a pure fallback.
- SL/T1 become deterministic per type → R:R is fixed and pre-verified ≥ 1.0.
- No re-picking of structural levels centrally → no risk of subtly changing T2 behavior.

---

## 6. Data flow

```
candle ─▶ detector.update() ─▶ signal{struct SL, struct T1, struct T2}
                                   │
                          _apply_per_type_levels(signal, settings)
                                   │   SL := entry ∓ pt.sl     (replace)
                                   │   T1 := entry ± pt.t1     (replace)
                                   │   T2 := struct if beyond T1 else entry ± pt.t2fb
                                   ▼
                          R:R gate (reward/risk ≥ 1.0)  ─▶  alert / trade
```

---

## 7. Test plan (TDD, before PR)

New `tests/unit/test_per_type_levels.py`:
1. Each setup type → SL/T1 land at exact `entry ± configured` distance, both directions.
2. T2 kept when detector's structural T2 is beyond new T1; fallback used when it isn't.
3. R:R gate passes for all 4 default configs (no suppression).
4. Unknown/missing setup_type → structural levels preserved (safety path).
5. Config: both profiles expose all 4 keys; values are the approved table.

Existing detector tests: unaffected (detectors unchanged). Existing engine R:R test: re-point to post-override values.

---

## 8. Risks & non-goals

- **Low n on 3 of 4 types** — shipped as defaults, re-tuned in ~1 month (2026-08). Logged in findings + memory.
- **T2 numbers drift from grid** — expected (structural runner). Acceptable and generally favorable.
- **Non-goals:** the 40/60 runner split and cost→BE stop live in execution/position_manager, NOT config — untouched here. This POC only sets the three price levels per type.
- **Backward compat:** if `per_type_levels` is empty, engine leaves structural behavior intact (no regression for any profile that omits it).

---

## 9. Rollout

1. Approve this POC.
2. Branch `feature/TASK-185-per-type-sl-target`.
3. TDD: write `test_per_type_levels.py` → red.
4. Implement config `SetupLevels` + field + engine helper → green.
5. Full suite green → PR → halt for review.
6. Dual-sync findings to `docs/` + Obsidian; update memory `[[ares-sl-target-optimization]]`.

**Approve as-is, or adjust any number / decision above.**
