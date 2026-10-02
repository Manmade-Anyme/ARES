# TASK-210 coverage and latency verification

**Date:** 2026-10-02 (Asia/Kolkata)
**Scope:** Historical verified commit `0653005` of PR #122, complete `system_one` production Python package.
**Later review fixes:** Polling/UUID changes were pushed without waiting for tests or CI, as requested. The counts below do not certify those later commits.
**Result:** Local automated coverage and regression gates PASS. Independent read-only P1 lifecycle audit CLEAN; the auditor independently reran all 204 focused tests. The preceding latency change's independent final audit was unavailable because that agent hit its usage limit.

## Exact coverage

```bash
python3 -m pytest tests/unit/test_task210*.py -q \
  --cov=system_one --cov-branch --cov-report=term-missing --cov-fail-under=100
```

**204 focused tests passed.** No coverage exclusions, missing statements, missing branches, or partial branches.

| Module | Statements covered | Branches covered |
| --- | --- | --- |
| `__init__` | 2/2 | 0/0 |
| `__main__` | 2/2 | 0/0 |
| consumer | 239/239 | 44/44 |
| context | 146/146 | 70/70 |
| Discord | 140/140 | 36/36 |
| Jev | 63/63 | 4/4 |
| **Total** | **592/592 (100%)** | **154/154 (100%)** |

The measured prior baseline was 67 tests and approximately 79% combined line/branch coverage. This follow-up adds 137 cases. No `reports/qa/baseline.json` existed; comparison uses the preceding verified full-suite result of 923 tests and 10 subtests. [Coverage summary](TASK-210_coverage-summary.json) contains the exact machine-readable counts. GitHub CI now runs the same strict coverage command after its full regression test.

## Behavioral assertions

- Injected snapshot, signal, persistence, and delivery delays verify that the snapshot metric is the actual query duration and not the linked signal lookup. Persistence/delivery measurements finish after their acknowledgments. A direct snapshot caller records unknown query duration as null.
- The actual migration ignores an optimistic client-supplied total. A 40-second-old event yields event-based prediction/alert metrics that include upstream queue age. Only durable confirmed delivery plus the matching result may publish successful-alert latency; uncertain delivery remains null. Repeated finalization pins the original sample and never changes delivery state.
- Restart resumes saved pending delivery, records its latency, and makes no additional Jev call. Metric acknowledgment failure cannot replay delivery.
- Startup, pre-open and after-close waiting, next-session resumption without rebootstrap, interrupts, malformed signals, database failures, lost markers/acknowledgments, and optional XGBoost failure exercise worker behavior through external database/provider boundaries.
- Context tests assert exact geometry/ratio thresholds, finite input handling, missing evidence, actual collector distance mapping, and wick/OI behavior. A redundant positive-ratio guard was removed because validated geometry already guarantees positive risk and reward; tests do not bypass validation to invent an impossible branch.
- Discord tests apply token/status filters to durable state and assert no transport without an eligible marker, no stale-owner writes, terminal uncertainty, and no duplicate send after persistence failure. Jev tests assert probability mapping and request deadlines.

## Jev lifecycle P1 verification

The unscoped `never` restart policy and post-close exit stranded the Jev Machine because the external cron starts only the trading Machine. Updated worker entrypoints wait after close and resume at the next session. Job recovery runs only inside the session, so there is no overnight database polling after bootstrap. Fly restart policies now target `app` (`never`) and `jev` (`always`) independently. Deployment docs explicitly retain the app-only cron and explain overnight running charges, running-state verification, and one-time startup after an operator stop.

Five selected lifecycle tests failed before the production change. After-hours startup, both module entrypoints, and an existing process crossing close into the next business session now pass. The two-session test verifies fresh signal predictions/deliveries in both sessions, no requests in between, and one bootstrap. `fly config validate --strict --config fly.toml` passed; no Fly deployment or external cron change was performed.

## Regression and SQL verification

- `python3 -m pytest tests/ -q --import-mode=importlib`: **1,060 passed, 10 subtests passed**, nine existing warnings. Default local import mode has the previously documented `tests.unit` collision.
- Local PGlite harness (`@electric-sql/pglite@0.5.8`): **48 PostgreSQL lifecycle assertions passed** against the actual migration with synthetic fixtures. Includes freshness/ownership, atomic result completion, permission checks, and latency finalization.
- `git diff --check` passed. Main/ML application modules are unchanged; worker import boundaries are retained.

## Limits

Python coverage does not instrument SQL or prove live provider behavior. PGlite runs serial queries and overrides session eligibility after verifying session boundaries; it does not prove simultaneous database connection races. Total latency includes metric-finalization request overhead and is null if confirmation/finalization is missing. Report missing and unsuccessful delivery counts alongside success percentiles. No live TypeSafe/Discord call, Supabase migration, credentials change, or deployment was performed. The initial unmerged migration is not a deployed-schema upgrade. CI results are reported separately on the PR.
