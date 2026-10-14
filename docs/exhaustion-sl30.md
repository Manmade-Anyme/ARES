# Exhaustion Reversal: 30-Point Stop

TASK-212 · October 14, 2026

| Setting | Value |
|---|---:|
| Initial stop distance | 30 index points |
| First target distance | 18 index points |
| Second target | Existing nearest structural level beyond T1; 40-point fallback |
| Minimum T1 reward/risk | 0.60 for exhaustion |
| Global minimum for other setups | 1.0 |

Both expiry and non-expiry profiles use this configuration. For entry 24,000: bullish SL 23,970 / T1 24,018; bearish SL 24,030 / T1 23,982. T2 resolution and moving the stop to entry after T1 retain their existing behavior.

`SetupLevels.min_rr_ratio` is optional. The existing engine gate uses it when configured and otherwise uses `settings.min_rr_ratio`. Stop placement remains centralized in `apply_per_type_levels`; the detector is unchanged. Stored open trades retain their saved levels. New signals use the new settings after deployment.

Historical fixed-entry replay supported testing this width: 165 recorded entries scored −47 under original stops and +54 at 30, preserving recorded targets, including older 24-point T1 trades. Scores are SL −1, T1 +1, T2 +2 total, not cash P&L. Wider stopped trades allow more adverse movement; candle-order and feed limitations remain. This configuration change does not introduce new monitoring or scoring code.

Rollback: restore the exhaustion table entry to `SetupLevels(10.0, 18.0, 40.0)`; omitting the override restores the global 1.0 threshold.

Validation: run `python3 -m pytest tests/unit/test_config_profiles.py tests/unit/test_per_type_levels.py tests/unit/test_engine_efficiency_gates.py -q`. On local environments where an installed `tests` package shadows this repository, add `--import-mode=importlib`.
