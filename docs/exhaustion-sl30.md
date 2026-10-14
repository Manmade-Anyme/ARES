# Exhaustion Reversal: 30-Point Stop

TASK-212 · October 14, 2026

| Setting | Value |
|---|---:|
| Initial stop distance | 30 index points |
| First target distance | 18 index points |
| Second target | Existing nearest structural level beyond T1; 40-point fallback |
| Minimum T1 reward/risk | Global 1.0 for all setups |

Both expiry and non-expiry profiles use this configuration. For entry 24,000: bullish SL 23,970 / T1 24,018; bearish SL 24,030 / T1 23,982. T2 resolution and moving the stop to entry after T1 retain their existing behavior.

The follow-up removes the per-setup R:R override introduced in PR #130. `SetupLevels` retains its original three fields and the engine uses the original global `settings.min_rr_ratio` gate. Relative to the code before PR #130, the only application change is exhaustion stop distance 10 → 30.

**Emission consequence:** T1 18 / SL 30 = 0.60, below the unchanged global minimum 1.0. Exhaustion candidates are therefore rejected in both profiles and both directions. This is the user-requested SL-only configuration; it does not bypass the gate, raise targets, or lower the global threshold. Stored open trades retain their saved levels.

Historical fixed-entry replay supported testing this width: 165 recorded entries scored −47 under original stops and +54 at 30, preserving recorded targets, including older 24-point T1 trades. Scores are SL −1, T1 +1, T2 +2 total, not cash P&L. Wider stopped trades allow more adverse movement; candle-order and feed limitations remain. This replay preserved historical entries and did not apply the restored global gate; its +54 score is not a projection of emitted trades under this configuration. This change does not introduce new monitoring or scoring code.

The [October 14 target audit](../reports/research/TASK-212_exhaustion-target-audit-2026-10-14.md) retains T1=18: with SL30, T1=18/24/30 scored +54/+39/+29 under the documented recorded-T2 approximation. The structurally reliable 121-entry comparison and intraday sensitivities also favor18. Higher T1 delays the move to breakeven and increases full stop-outs. No additional application changes were needed.

Rollback: restore the exhaustion table entry to `SetupLevels(10.0, 18.0, 40.0)`. The original global 1.0 threshold is already in use.

Validation: run `python3 -m pytest tests/unit/test_config_profiles.py tests/unit/test_per_type_levels.py tests/unit/test_engine_efficiency_gates.py -q`. On local environments where an installed `tests` package shadows this repository, add `--import-mode=importlib`.
