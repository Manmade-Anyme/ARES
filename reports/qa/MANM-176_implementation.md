# MANM-176 — Implementation verification

Date: 2026-10-16
Status: PASS for approved paper-runtime scope. New live dispatch remains blocked.

## Results

- Full Python suite: `python -m pytest tests/ -q --import-mode=importlib` — **1,465 passed, 45 subtests passed**, 12 dependency warnings.
- Independent review follow-up: **88 persistence/dispatch/order tests passed**; reviewer approved after the terminal-deduplication regression was fixed.
- Focused calculation, execution, dispatch, persistence and alert/report coverage run: **218 passed**, **100% statement and branch coverage** for `options_math`, `execution.order_manager` and `execution.signal_dispatch` (562 statements, 224 branches; `--cov-fail-under=100`). Live operation is not certified.
- Required System One coverage command from CI: **398 passed, 35 subtests passed**, **100%** statement/branch coverage.
- `git diff --check`, Python compilation and ADR/document mirror comparison passed.
- Installed Dhan SDK `place_super_order` signature matches the tested transport payload. Tests use fake broker/database boundaries; no account orders or production data writes occurred.

## Environment note

The local Python 3.14 environment contains another installed `tests` package. Default pytest import mode fails collection for three pre-existing cross-test imports (`tests.unit`). `--import-mode=importlib` resolves that namespace collision without changing unrelated application code or test packaging. CI uses its existing clean Python 3.10 environment.

## Reviewed behavior

Capital and quote rejection, signed CE/PE Greeks, ATM-distance ranking, expiry-close validation, tick ordering and capped nonnegative sizing; full-chain broker identities/quotes; paper LIMIT fills, pending invalidation, whole-lot T1/T2 booking, observed-bid exits; final-lot reporting and safe legacy data; delayed-fill timestamp/ML binding; pending dedupe; isolated persistence retries and live dispatch rejection.

Review follow-up regressions verify synchronized CE/PE fill spots and break-even levels, invalid quote spot rejection, first-quote stop-loss persistence, registration retries after closure, delayed canonical ML binding in both schema modes, and terminal retry records excluded from pending deduplication. The initial persistence regressions and terminal-deduplication regression failed before their fixes.

The independent integration review findings and fixes are recorded in `reports/debug/MANM-176_integration-review.md`. The math review is recorded in `reports/qa/MANM-176_math.md`.

## Scope decision and limits

The user explicitly chose **Keep live activation blocked**. The application runs paper execution. The Dhan transport is tested independently, while `OrderManager` rejects new live orders with `LIVE_RUNTIME_UNSUPPORTED` before submission. Complete live exit/cancel/partial-fill, overnight DAY-order protection, expiry settlement and deployment preflight remain for a reviewed follow-up design. ATR trailing is deferred. Current spot outcome scoring remains separate from option simulation and estimated option reports.

Repository docs and CHANGELOG are included in PR #136. Obsidian production sync follows the pipeline's manual-merge gate.

## Carried-trade restart follow-up

Startup restores persisted analytics context by active trade ID, including current `options_sizing` and last saved paper execution telemetry. Six regression cases failed before the fix and passed afterward; an additional outage case confirms active monitoring still initializes. Both schema modes exercise entry, telemetry update, restart, lifecycle alert handoff and option estimate formatting. Legacy/malformed context, unrelated trades and terminal reconciliation are covered. The fake RPC now matches production by storing context only in analytics, and selected columns are projected at the test database boundary.

Primary-agent implementation review and QA completed after sub-agents reached their usage limit; independent design review found no blocking issues. Full suite: 1,465 tests plus 45 subtests. Feature coverage: 218 tests, unchanged 100% statement and branch coverage for the three covered calculation/execution modules. Required System One gate: 398 tests plus 35 subtests, 100%. Restored execution context is historical telemetry; simulation and live execution are not resumed at startup.

## Capital-independent pricing follow-up — 2026-10-20

Implemented and reviewed directly in Codex at the user's request, without Antigravity or sub-agents. Updated the working repository to merged PR #140 (`ad293fa`) before final verification.

- Reproduction: 18 new regression cases failed before the fix because unavailable capital erased option prices and zero-lot alerts suppressed their fields; two insufficient-positive-capital pricing cases already passed.
- Behavior: CE and PE entry, stop loss, T1, T2, Greeks and risk calculations remain identical across funded, zero, debit, unavailable and invalid balances. Capital controls allocation only. Zero-lot brackets remain rejected by execution; malformed quotes still fail validation even with zero capital.
- Presentation and persistence: text and Discord alerts retain option prices with zero lots, T1 no longer advertises one planned lot when allocation is zero, and persisted sizing context and summary retain prices and allocation diagnostics.
- Full suite: `python -m pytest tests/ -q --import-mode=importlib` — **1,608 passed, 45 subtests passed**, 12 dependency warnings. The import-mode environment note above still applies.
- Focused coverage: `python -m pytest tests/unit/test_options_math.py tests/unit/test_order_manager.py tests/unit/test_manm176_dispatch.py tests/unit/test_manm176_persistence.py tests/unit/test_manm176_review_persistence.py tests/unit/test_options_alert_reporting.py -q --import-mode=importlib --cov=options_math --cov=execution.order_manager --cov=execution.signal_dispatch --cov-branch --cov-report=term-missing --cov-fail-under=100` — **254 passed**, **100% line and branch coverage** (561 statements, 222 branches).
- No deployment or live-account validation performed. Broker, Discord and database boundaries in tests use fakes.
