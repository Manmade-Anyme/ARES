# QA report — TASK-211

Date: 2026-10-09. Verdict: PASS for local implementation and independent review. Native concurrent CI and supervision passed for aa869a1; final outcome-persistence revision CI and production rollout pending.

| Check | Result |
|---|---|
| Full regression suite | 1254 passed, 45 subtests passed |
| Confirmed-signal + watch worker suite | 398 passed, 35 subtests passed |
| Worker line coverage | 100%: 891/891 statements |
| Worker branch coverage | 100%: 266/266 branches |
| Isolated PostgreSQL watch lifecycle | 77 single-connection PGlite checks passed |
| Native concurrent PostgreSQL generation checks | 11 passed in CI for aa869a1; final outcome-persistence revision CI pending |
| Python 3.10 syntax parsing | 12 changed/new modules passed |
| Workflow YAML / diff whitespace | Valid / clean |
| Runtime dependency additions | None |
| New config environment overrides | Removed and regression-tested |
| Shared launcher identity | Real shell execution verifies equal fresh child UUID/start-time metadata and no child fork on generation failure |
| Review-fix application guard | Independent 46-test application run passed; modified guard lines and branches fully covered |
| Consumer supervision | 6 focused shell/lifecycle tests passed; failed consumer is replaced with the same metadata and stopped on app exit |
| Final watch outcome persistence | 50 application tests passed; blocked writer proves RESOLVE/CANCEL persist before helper return; timeout/error preserves accepted signal |

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

The second review comment also reproduced: a delayed old-producer enqueue after restart cleanup still created an ACTIVE watch. The pending migration now stores the active UUID and immutable launcher start time in a service-role-only singleton. Recovery advances only to a newer start, rejects delayed older recovery including its first call, and remains idempotent for the current metadata. Enqueue and base-action claims take the shared state lock before watch locks; recovery takes the exclusive state lock first. Tests verify new retired-run observations cannot be inserted and retired-run rows cannot be polled or claimed for new inference/base delivery, even for an ACTIVE-looking legacy row. Previous-run cancellation and archival remain available, and the unfenced old recovery signature is removed. Existing confirmed-signal schema and prediction behavior remain unchanged.

Independent QA and final code review passed for the corrections: runtime edits are limited to launcher, outbox, watch consumer, the final signal-delivery lifecycle helper and their pending watch migration, with CI verification added. The existing parallel-worker backoff test was corrected to allow both workers to reach the same 30-second backoff; it still asserts the backoff and shutdown outcome. Worker coverage above is not a claim of whole-application coverage: a separate application-test coverage run measured the existing outbox at approximately 91%, with all modified initializer lines and branches covered. No unrelated uncovered code was changed.

`tests/integration/task211_watch_generation.py` uses separate native PostgreSQL sessions to hold real generation locks while an enqueue/takeover or inference/base-delivery claim overlaps. CI provisions a disposable PostgreSQL 17.6 container and passes only its container ID; the harness cannot consume application database credentials. Independent review checked SQL lock order and Docker/psql process handling. These concurrent checks have not executed locally and must pass CI before merge; the 77 PGlite checks do not establish multi-session lock execution.

Native verification and supervision passed in [CI run 37958995041](https://github.com/Manmade-Anyme/ARES/actions/runs/37958995041) for aa869a1: 1250 Python 3.10 tests, 100% worker coverage, 77 lifecycle checks and 11 overlapping PostgreSQL checks. The subsequent final outcome-persistence correction changes no SQL; its complete CI rerun remains pending push.

A third valid review comment identified permanent watch-delivery loss if the consumer process exited while ARES continued. The actual shell regression failed before correction (1 failed, 2 passed): the first consumer exited with status 9 and the waiting application exited with status 13 because no replacement arrived. The launcher now reaps and restarts the consumer on its existing two-second monitor loop, reusing the same UUID/start time and checking ARES is still alive before spawning. The application is not restarted. The replacement stays alive in the regression until the launcher kills it on application exit, verifying shutdown of the latest child. This correction touches only launcher behavior and its test/docs, with no application, outbox, model or SQL changes. Independent review passed.

A fourth review comment identified queued final RESOLVE/CANCEL returning before the healthy writer persisted it. Four focused regressions failed before correction, including a blocked writer after accepted/rejected Discord delivery. The helper now captures the existing episode UUID before acknowledgment and awaits a bounded five-second persistence flush after terminal dispatch. It skips this wait without a watch/outbox, logs false/error outcomes, and preserves the Discord result without resending or waiting for inference. Independent diff review passed; 50 application tests passed. This reduces the return-before-persistence gap but cannot make Discord and database commit atomic under hard termination or database failure.

Recommendation: ready for PR review, subject to final outcome-persistence revision CI. Apply `migrations/2026-10-08-task211-oi-watch-jev.sql` before feature deployment and verify worker/outbox readiness after deployment. `.env` retains credentials; change the new knobs only in `config_profiles.py`.
