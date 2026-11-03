# QA Report — TASK-20261103 Tuesday Expiry Profile Tuning

**Verdict:** LOCAL PASS; latest GitHub CI pending on the updated branch head.

## Verification

| Check | Result | Evidence |
|---|---|---|
| Expiry/non-expiry profile assertions | PASS | 18 tests passed in `tests/unit/test_config_profiles.py` on code commit `82f0ded` |
| Expiry breakout threshold behavior | PASS | New tests verify 4% growth is below the expiry threshold and 5% growth is active |
| Whitespace validation | PASS | `git diff --check` clean |
| Full repository test suite | PASS (local) | 2,124 passed, 45 subtests passed on Python 3.14.6 |
| `config_profiles.py` line and branch coverage | PASS | 100% (79 statements, 0 missed; 8 branches, 0 partial) from the full local suite |
| Latest GitHub CI | PENDING | Previous run failed on the stale 15% expectation; updated branch head must complete a new CI run |
| Full-engine expiry replay / outcome comparison | NOT RUN | Existing Dhan replay covers only the OI-wall detector/filter subset and exhaustion volume/body gate |

## Scope and regressions

The profile tests cover the 16 approved expiry settings, selected unchanged settings, non-expiry settings, and profile application. The obsolete TASK-174 expectation was corrected to 5%; two detector-path checks cover behavior below and at the inclusive threshold. No detector code changed. The partial replay does not establish P&L or win-rate impact; full CI on the updated head remains the merge check.

## Recommendation

Review the new GitHub CI result before merge.
