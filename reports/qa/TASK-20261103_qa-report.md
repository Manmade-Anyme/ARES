# QA Report — TASK-20261103 Tuesday Expiry Profile Tuning

**Verdict:** PENDING — keep PR #154 in draft until checks on the latest PR commit finish.

## Verification

| Check | Result | Evidence |
|---|---|---|
| Expiry/non-expiry profile assertions | PASS | 18 tests passed in `tests/unit/test_config_profiles.py` on code commit `82f0ded` |
| Whitespace validation | PASS | `git diff --check` clean on `82f0ded` |
| Full repository test suite | PENDING | GitHub Actions PR run was in progress when this report was prepared; the report commit will advance the PR head and trigger a new run |
| Coverage comparison | PENDING | Await the latest CI run; no coverage result is claimed here |
| Full-engine expiry replay / outcome comparison | NOT RUN | Existing Dhan replay covers only the OI-wall detector/filter subset and exhaustion volume/body gate |

## Scope and regressions

The unit assertions cover the 16 approved expiry settings, selected unchanged expiry settings, unchanged non-expiry settings, and profile application. No detector implementation changed. The audit report documents the replay limits and untested engine interactions.

No full-suite regression conclusion is available until the latest GitHub Actions result completes. The partial replay does not establish P&L or win-rate impact.

## Recommendation

Keep the PR as draft. Review the latest CI result and coverage output before marking it ready for human review or merge.
