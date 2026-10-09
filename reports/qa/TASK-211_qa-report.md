# QA report — TASK-211

Date: 2026-10-09. Verdict: PASS for local implementation; production rollout pending.

| Check | Result |
|---|---|
| Full regression suite | 1243 passed, 45 subtests passed |
| Confirmed-signal + watch worker suite | 387 passed, 35 subtests passed |
| Worker line coverage | 100%: 888/888 statements |
| Worker branch coverage | 100%: 264/264 branches |
| Isolated PostgreSQL watch lifecycle | 60 checks passed |
| Python 3.10 syntax parsing | 12 changed/new modules passed |
| Workflow YAML / diff whitespace | Valid / clean |
| Runtime dependency additions | None |
| New config environment overrides | Removed and regression-tested |
| Shared launcher identity | Real shell execution verifies equal fresh child UUIDs and no child fork on UUID-generation failure |
| Review-fix application guard | Independent 43-test application run passed; modified guard lines and branches fully covered |

Commands:

```sh
python -m pytest tests/ -q --import-mode=importlib
python -m pytest tests/unit/test_task210*.py tests/unit/test_task211*.py -q --cov=system_one --cov-branch --cov-report=term-missing --cov-fail-under=100
PGLITE_MODULE_URL=file:///private/tmp/ares-task211-sql/node_modules/@electric-sql/pglite/dist/index.js node tests/integration/task211_watch_lifecycle.mjs
git diff --check
```

The local Python 3.14 environment has an installed-package collision with the repository's `tests.unit` namespace; importlib mode avoids that collection issue. CI continues to use Python 3.10 matching Docker. Local checks establish Python 3.10 syntax and variable-fraction timestamp compatibility, not an executed Python 3.10 test run. CI adds isolated SQL lifecycle execution using pinned test-only PGlite 0.5.8.

Twelve existing dependency warnings remain in the full run, including Supabase deprecations, pandas/SHAP warnings and the retained XGBoost model-version warning. No test failure or skip remained.

Behavior verified: exactly one added field in the first watch, unchanged original embed values, bullish/bearish thresholds, missing/stale/as-of evidence, frozen input and model metadata, no inference in application dispatch, unavailable fallback, atomic ownership, timeout/retry pinning, cancellation before/after/in-flight send, supersession closure, restart/UNKNOWN recovery and backend-only privileges. The full suite used fakes and isolated storage; production tables and Discord were untouched.

PR review identified a reproducible startup race: watch workers could infer or send a prior-run watch before application startup canceled it. The regression failed before correction (7 failed, 64 passed), including dispatch of the prior watch. The launcher now supplies one ephemeral UUID to both processes, and each watch loop completes recovery before polling. Tests verify prior-run suppression, current-run preservation with late or restarted consumers, recovery failure followed by successful retry, and delivery of new current-run observations after recovery. An uncoordinated application retains direct watch delivery with an unavailable assessment; an uncoordinated consumer skips only watch workers. Existing confirmed-trade Jev is unchanged.

Independent QA and final code review passed for the correction: production edits are limited to launcher, outbox and watch consumer. The existing parallel-worker backoff test was corrected to allow both workers to reach the same 30-second backoff; it still asserts the backoff and shutdown outcome. Worker coverage above is not a claim of whole-application coverage: a separate application-test coverage run measured the existing outbox at 90.78%, with all modified initializer lines and branches covered. No unrelated uncovered code was changed.

Recommendation: ready for PR review. Apply `migrations/2026-10-08-task211-oi-watch-jev.sql` before feature deployment and verify worker/outbox readiness after deployment. `.env` retains credentials; change the new knobs only in `config_profiles.py`.
