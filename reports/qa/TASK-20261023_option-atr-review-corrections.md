# TASK-20261023 / PR #147 correction QA report

Date: 2026-10-23
Verdict: **PASS for local automated checks; production activation remains gated**.
Reviewed feature head: `6788dcb9dd392299d35cec994a072fd2278da23a`.
Base: `964566331d49037566228a8b7823de57476425a4`.
Correction commit: the commit containing this report on PR #147; exact SHA and CI run are recorded in PR metadata.

## Verification

| Check | Result |
|---|---|
| Complete frozen-code Python suite | **1,876 passed, 45 subtests passed**, 99.64 seconds, Python 3.14.6. Twelve existing library/deprecation warnings; no failures or skips. |
| Actual isolated PostgreSQL migration suite | **29 checks passed**, using the existing pinned PGlite 0.5.8 test engine. |
| Changed Python source compilation | Passed. |
| Whitespace/error check | `git diff --check` passed. |
| Initial bracket/lot/risk preservation | Actual enrollment tests preserve index entry/SL/T1/T2, original Greeks bracket and suggested lots. All release prerequisites must pass. |
| Incident source replay | Real application monitor + PositionManager, explicit injected ledger, correct contracts and source chronology; compact trace committed beside this report. |
| Independent review | Runtime reviewer confirmed resolved findings; storage/SQL/ML reviewer findings were reproduced and fixed. Its final additional pass hit the usage limit; root verified the final conflict-card correction and complete suite. |
| Production | No SQL applied, activation, deployment, restart, live Discord test or broker action. PR remains for human review/merge. |

The reviewed head had four reproduced failing tests. They now pass with legacy behavior preserved behind the disabled gate and equivalent new-policy regression coverage. Synthetic forced incident exits were replaced with fixture-driven outcomes.

## Coverage

Production Python totals exclude test files from the collected coverage JSON; no new coverage exclusions were added.

| Metric | Reviewed head | Corrected code | Default pipeline minimum |
|---|---:|---:|---:|
| Statement coverage | 88.74% (8,234 / 9,279) | **89.05% (8,885 / 9,977)** | 80% |
| Branch coverage | 83.20% (2,664 / 3,202) | **84.12% (2,946 / 3,502)** | 70% |

Coverage increases on both axes. The project target of 100% is **not achieved**: 1,092 statements and 556 branches remain uncovered across production modules. This task does not claim repository-wide coverage completion.

The baseline uses the reviewed commit exported to an isolated directory with the same coverage settings. Its Git-ignore fixture initially lacked repository metadata; the fixture passed after initializing that disposable directory. That environment check does not affect the production coverage comparison.

## Commands and local environment

CI uses the repository's normal `python -m pytest tests/ -q` on Python 3.10. Local Python has an unrelated installed `tests` package; the local runner explicitly bound the repository's `tests` and `tests.unit` namespaces and prepended the repository root before invoking:

```text
pytest tests/ -q --import-mode=importlib -p no:cacheprovider --tb=short
       --cov=. --cov-branch --cov-report=term
       --cov-report=json:/tmp/ares-pr147-frozen-coverage.json
```

All Supabase/Dhan/Discord test values were dummy credentials/endpoints. No production service was used by tests. The isolated SQL command was:

```bash
PGLITE_MODULE_URL=file:///tmp/ares-pr147-pglite/node_modules/@electric-sql/pglite/dist/index.js node tests/integration/option_lifecycle_sql.mjs
```

CI installs the already used pinned PGlite dependency and executes this script as well. It verifies denied public API roles, backend access, revisions/idempotency, immutable identity, upward stop/score invariants, alert claim/ack safety, nullable index exits and a second-table failure rolling back both ledgers.

## Remaining release limits

- Exact Dia script/feed parity remains unverified; the candidate previously differed by about ₹0.57 even with multi-session data. Defaults keep `option_atr_enabled=False` and `option_atr_parity_version=""`. Enabling additionally requires the explicit backend service-role key.
- No carried trade is converted automatically. The offline preview accepts exported rows and sourced registration/history/schema/parity evidence, emits proposals only, and always requires human approval for legacy conversion. Closed history and historical alerts remain unchanged.
- Session-open evidence comes from valid source bars or trade registration. Cold start with all providers unavailable is unknown/log-only; no exchange holiday calendar is invented.
- Durable `SENDING`/`AMBIGUOUS` deliveries require reconciliation rather than a blind retry. Webhook delivery cannot honestly promise end-to-end exactly once.
- Source-resolution replay ends at the committed fixture cutoff and does not prove live fills, chart parity, native PostgreSQL multi-process performance or production behavior.
- PR CI runs against the pushed correction commit; see the PR checks for current status. No deployment claim follows from local tests or PR CI.


## Subsequent PR comment follow-up

The later timestamp/outbox fixes and the owner's explicit expiry-scope decision are verified in [the comment disposition report](TASK-20261023_pr147-comment-disposition.md). Its 1,880-test / 60-SQL-check results supersede the earlier test counts above for the follow-up commit; exact head and CI status remain in PR metadata.
