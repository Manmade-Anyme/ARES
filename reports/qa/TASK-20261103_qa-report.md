# QA Report — TASK-20261103 Tuesday Expiry Profile Tuning

**Verdict:** PASS — all unit tests and coverage thresholds pass cleanly locally.

## Verification

| Check | Result | Evidence |
|---|---|---|
| Expiry/non-expiry profile assertions | PASS | 18 tests passed in `tests/unit/test_config_profiles.py` on code commit `82f0ded` |
| Whitespace validation | PASS | `git diff --check` clean |
| Full repository test suite | PASS | 2122 passed; fixed legacy assertion in `tests/unit/test_task174_oi_scoring.py` expecting pre-tuned 15.0% writer growth |
| Coverage comparison | PASS | 100% System One test coverage verified locally (398 passed) |
| Full-engine expiry replay / outcome comparison | NOT RUN | Existing Dhan replay covers only the OI-wall detector/filter subset and exhaustion volume/body gate |

## Scope and regressions

The unit assertions cover the 16 approved expiry settings, selected unchanged expiry settings, unchanged non-expiry settings, and profile application. No detector implementation changed. The audit report documents the replay limits and untested engine interactions.

Initial CI run failed on `tests/unit/test_task174_oi_scoring.py::TestWritersActiveThresholdConfig::test_expiry_profile_is_fifteen_percent` due to a hardcoded legacy assertion of 15.0%. This was updated to assert the tuned 5.0% `breakout_writers_active_min_pct`. The complete test suite now passes locally.

## Recommendation

Ready for human review and merge.
