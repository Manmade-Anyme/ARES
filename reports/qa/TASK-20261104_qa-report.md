# QA Report — PR #156 Terminal Paper Exit Recovery

## P2: route pinned depth bids to pending paper exits

- Pinned Dhan and selected-option chain observations now preserve finite positive bid/ask depth; crossed depth cannot be used for an exit.
- Focused runtime, quote-service, price-fetcher, evidence, dispatcher, and selected-quote regressions: **141 passed** after the final fail-closed guards. Coverage includes source/receipt freshness, contract matching, crossed depth, paper exit at the observed bid, non-v2 isolation, selected-option fallback, and polling of restored terminal `EXIT_PENDING` routes.
- Repository suite: **2,511 passed, 45 subtests passed, 12 warnings** in 172.03 seconds. Final defensive guards and the selected-option fallback were then checked in the focused suite. Compilation and `git diff --check` pass; exact-head Actions awaits the push.
- No production data, live provider request, broker order, deployment, or worker start was used.

## P2: require ML-label table access for v2 readiness

- Added `SELECT` and `UPDATE` checks for `public.ml_collection` to the v2 capability RPC. The isolated PostgreSQL regression removes each privilege independently and asserts `ready=false`.
- Repository suite: **2,507 passed, 45 subtests passed, 12 warnings** in 176.02 seconds. `node --check tests/integration/option_accounting_sql.mjs` and `git diff --check` passed.
- PGlite was unavailable locally; exact-head Actions run **#632 passed** on `bcde770`, including both PostgreSQL lifecycle checks. That run predates the current depth-bid follow-up, which needs its own latest-head run. No production data, provider requests, broker orders, deployment, or worker start used.

## P2: fractional-fill quote overlap

- Regression covers a ₹160 Dhan quote sourced at 10:20:00 against a 10:20:00.500 confirmed fill, quote recovery before the next timestamp, and a later T1 touch. The ambiguity persists through serialization and retry without a route or milestone finalization. A 10:20:00 source second ending before a 10:20:01.500 fill remains rejected.
- Focused conflict and premium modules: **147 passed, 2 warnings**. Full isolated `tests/`: **2,507 passed, 45 subtests passed, 12 warnings** in 174.58 seconds. Compilation and `git diff --check` passed; exact-head Actions pending.
- No production data, provider requests, broker orders, deployment, or worker start used.

## P2: T1-tied quote after ATR seeding

- CE and PE fail-first cases use the reported ₹161.05 initial stop, ₹167.20 entry, ₹196 T1-seeded ATR stop, and ₹190 quote received two seconds after its T1 source time.
- Focused conflict and premium modules: **145 passed, 2 warnings**. The new quote remains durable through serialization, creates no execution route, and prevents the following T2 tick from committing an assumed allocation.
- Full isolated `tests/`: **2,505 passed, 45 subtests passed, 12 warnings** in 175.30 seconds. Compilation and `git diff --check` passed; exact-head Actions pending. No production data, provider requests, broker orders, deployment, or worker start used.

## P1: retain concurrent protection across stale lifecycle writes

- Four fail-first cases reproduce lost protective intent after T2 or terminal writes, immediately and after outage. Expanded coverage includes deferred exits and queued stronger stops.
- Focused conflict and premium lifecycle modules: **143 passed, 2 warnings**. Assertions verify pending routes, immutable first trigger, unresolved allocation, retained observations, serialization, queue drainage, and a later proven route following cost-only uncertainty.
- Full isolated `tests/`: **2,503 passed, 45 subtests passed, 12 warnings** in 174.86 seconds. Compilation and `git diff --check` passed; exact-head Actions pending. No production data, provider requests, broker orders, deployment, or worker start used.

## P2: quote-watermark recovery across conflicting writers

- Three fail-first replays cover T2, terminal exit, and T2 with a newer local quote during an outage. The ledger enforces the SQL monotonic-watermark rule.
- Focused conflict and premium lifecycle modules: **130 passed, 2 warnings**. Checks cover drained queues, persisted/live/restored maxima, matching alert snapshots, and unchanged terminal exit evidence.
- Full isolated `tests/`: **2,490 passed, 45 subtests passed, 12 warnings** in 174.21 seconds. Compilation and `git diff --check` pass; exact-head Actions verification pending. No production data, broker orders, deployment, or worker start used.

## P2: coherent stop metadata after revision-conflict recovery

- Four fail-first milestone/terminal and immediate/outage-retry cases left the database at T1 with an invalid ₹180 `BREAK_EVEN_COST` snapshot queued. The test ledger now enforces the v2 SQL source/price invariants.
- Conflict recovery plus premium lifecycle modules: **127 passed, 2 warnings**. Cases verify adoption of the full committed stop revision in live, persisted, restored and alert contexts; the queue drains, later protection uses ATR, and recorded break-even exit provenance remains intact.
- Full isolated `tests/`: **2,487 passed, 45 subtests passed, 12 warnings** in 178.58 seconds. Compilation and `git diff --check` pass; Actions for the pushed revision are pending.
- No production data, provider requests, broker orders, deployment, or worker start was used.

## P2: T1 bar tied to an ATR stop seeded above cost

- Four fail-first CE/PE same/split-batch cases emitted an unwarranted `ATR_SL_HIT` for a ₹190 T1-tied bar against the newly seeded ₹196 stop, despite initial SL ₹161.05 and entry cost ₹167.20.
- Focused module: **102 passed, 2 warnings**. Corrected cases retain `T1_ORDER_UNKNOWN/ATR_STOP_ONLY_BREACH` and the seeded level, persist/restore it, deduplicate unchanged retries, survive recovery, and route a later true breach while reference finalization remains deferred.
- Full isolated `tests/`: **2,483 passed, 45 subtests passed, 12 warnings** in 175.12 seconds. Compilation and `git diff --check` pass; Actions for the pushed revision are pending.
- No production data, provider requests, broker orders, deployment, or worker start was used.

## P2: reconcile quote-first evidence before same-time milestones

- All four fail-first replays committed T2 incorrectly when the ₹165 cost-only quote arrived before the matching index tick. The corrected cases cover direct replay, an intervening newer quote, restart with pending evidence, and a legacy last-quote snapshot.
- Focused lifecycle module: **98 passed, 2 warnings**. Invalid cached contract and receipt-lag evidence is rejected; valid cached evidence defers T2 without an execution route, preserves the maximum watermark, persists ambiguity, and clears the pending cache in the T1 snapshot.
- Isolated worktree full `tests/`: **2,479 passed, 45 subtests passed, 12 warnings** in 176.86 seconds. Compilation and `git diff --check` pass. Actions for the pushed revision are pending.
- Work was isolated from the separate active IV-breakout branch. No production data, provider requests, broker orders, deployment, or worker start was used.

## P2 findings: source-second price changes and delayed T1/T2 cost uncertainty

- Fail-first replays reproduced lost initial/BE/ATR stop breaches for CE/PE price changes at one source second, lost durable price revisions, and missing delayed T1/T2 cost-only ambiguity with or without a newer watermark.
- Final focused module: **92 passed, 2 warnings**. Coverage includes identical retry deduplication, returning to an earlier price, durable/recovered ambiguity, later proven protection without reference finalization, first-trigger retention through recovery, preservation of prior cost-only evidence when upgraded, and both same-batch quote orders with a terminal-safe ledger.
- Full `tests/`: **2,475 passed, 45 subtests passed, 12 warnings** in 176.84 seconds. Compilation and `git diff --check` pass. Actions for the pushed revision are pending.
- The delayed split-batch case keeps committed T2 recorded and explicitly marks its allocation as disputed; it creates no route from the cost-only quote itself. No production data, provider requests, broker orders, deployment, or worker start was used.

## P2: preserve a newer quote watermark for delayed T2 protection

- The fail-first replay committed T2 at 09:20:00, then ₹180 at 09:20:01, then received ₹160 sourced at T2. It reproduced the regressed quote watermark.
- The corrected regression enforces the lifecycle RPC's monotonic-watermark rule in the test ledger, verifies committed `T2_ORDER_UNKNOWN` and pending route state, preserves 09:20:00 in the protective trigger, restores that state from serialization, and confirms no write remains queued.
- Focused lifecycle module: **77 passed, 2 warnings**. Full `tests/`: **2,460 passed, 45 subtests passed, 12 warnings** in 175.92 seconds. Compilation and `git diff --check` pass; Actions for the pushed revision are pending.
- No production data, provider requests, broker orders, deployment, or worker start was used.

## P1: T2 quote before the active stop revision

- Fail-first replay reproduced `ATR_SL_HIT` for a ₹198 quote sourced at T2 (10:29:59) after the 10:30 bar raised the stop from ₹196 to ₹201.50. Both ordinary ingestion and the equal-time quote-watermark exception failed before the fix.
- The shared helper now rejects that earlier quote without creating an ambiguity, trigger, or pending route, retaining the current ₹201.50 stop. Existing exact-raise-time prior-stop regressions remain green.
- Focused lifecycle module: **76 passed, 2 warnings**. Full `tests/`: **2,459 passed, 45 subtests passed, 12 warnings** in 176.86 seconds. Compilation and `git diff --check` pass. Actions for the pushed revision are pending.
- No production data, provider requests, broker orders, deployment, or worker start was used.

**Verdict:** PASS — local full and focused tests pass; exact-head GitHub Actions for the depth-bid follow-up is pending push.

## Verification

| Check | Result | Evidence |
|---|---|---|
| Focused current reviewer regressions | PASS | 141 passed |
| CI test scope (`tests/`) | PASS | 2,511 passed, 45 subtests passed, 12 warnings in 172.03s |
| New monitoring-gate statements | PASS | The terminal-route helper and protected-fetch, REST, and tick-poll gates were exercised by the focused coverage run |
| New monitoring-gate branches | PASS | All three route-queue paths reached the existing dispatcher with no active trades or new candle |
| Python compilation | PASS | `python -m compileall -q position_manager.py execution/signal_dispatch.py main.py` |
| Whitespace validation | PASS | `git diff --check` |
| Latest-head GitHub Actions | PENDING | Push is required before CI can run against the depth-bid follow-up |

The tests emitted existing dependency/runtime warnings. The root `pytest -q` command also collects ignored scratch scripts that call Supabase during import and failed collection under the offline sandbox; the repository CI command `python -m pytest tests/ -q` passed. No live service restart, broker interaction, or production trade was performed. The PR remains for human review; no approval or merge is implied by this report.

## Follow-up review verification

- Focused recovery, dispatch, option lifecycle, and commit-boundary suite: **178 passed, 2 warnings** on the final code and test tree.
- Full local suite: **2,351 passed, 45 subtests passed, 12 warnings** in 175.73 seconds. It was collected before the final direct terminal-handler regression was added; production code did not change afterward, and that regression passed in the focused run above.
- Both changed executable statements and both changed branch arcs in each of `execution/signal_dispatch.py` and `position_manager.py` were exercised; the modified multiline guard anchors in the dispatcher were also covered in both directions.
- The terminal-state regression uses an isolated ledger stub enforcing the migration's rejection of new lifecycle transitions after closure. It covers option and index exits, a lost terminal commit response, retry, and preservation of pending metadata when no earlier stop closes the trade. It does not establish live database behavior.

## Index-guard outage verification

- Focused option lifecycle, execution routing, and persistence suite: **216 passed, 2 warnings** on the final production code, before the three additional validation regressions below.
- Full local suite on the final production code: **2,390 passed, 45 subtests, 12 warnings** in 174.71 seconds. Three additional validation regressions were added after collection; all passed in the subsequent 41-test guard-focused run.
- Combined coverage from the full and guard-focused runs exercised **51/51 changed executable statements and 26/26 changed branch arcs** across `execution/signal_dispatch.py`, `main.py`, `models.py`, and `position_manager.py`.
- Regressions cover CE and PE runners, T1 partial exits awaiting a bid, legacy snapshots, missing history, first-intent preservation, failed synchronization, restart before routing and after acknowledgement, stale/pre-T1 observations, a T1-spanning candle, a same-timestamp watermark, and malformed-route isolation.
- GitHub Actions run #596 passed for the earlier commit `efa67a5`; it does not cover this follow-up change. A run for the new head is required after push.

## ATR raise timestamp review

- GitHub Actions run #597 passed for the preceding commit `bd5b162`, including database integration checks; it does not cover this timestamp fix.
- The 221-test focused lifecycle and routing suite passed on the fix. Two additional replay cases, added after its collection, passed separately and verify matching reference outcomes when an ATR bar and tied quote arrive together or in separate batches.
- The full local suite on the final production code passed: **2,396 tests, 45 subtests, 12 warnings** in 175.56 seconds. The two replay cases were added after that suite was collected and passed separately.
- All **6 changed executable statements and 4 changed branch arcs** in `position_manager.py` were covered by the full suite.
- CE and PE regressions check rejection of a tied ATR quote and a subsequent fresh ATR-stop hit. A separate equality regression verifies that a cost-stop touch still reports `STOPPED_OUT_AT_BE` with zero runner points.

## T1-candle replay review

- Focused option lifecycle, routing, and persistence suite: **228 passed, 2 warnings** on the replay safeguard.
- The production timeline replay was exercised for CE and PE with missing option history, both when only the pre-T1 wick touches the guard and when the completed close touches it. Direct-call regressions also pass.
- The full local suite passed: **2,402 tests, 45 subtests, 12 warnings** in 181.73 seconds. The changed statement and both branch arcs were covered.
- GitHub Actions run #598 passed on preceding commit `a445314`, including database integration checks; a new run is required for this replay safeguard.

## Guard during unresolved reference ordering

- The 68-test guard-focused suite passed. Twelve new end-to-end cases cover CE and PE, tick and completed-candle observations, and unresolved premium-bar, premium-quote, and index ambiguities while ATR is disabled; two cases also verify persistence when the observation was already retained.
- Additional checks retain an existing premium route and reject reference-only, stale, or unconfirmed execution inputs, including a candle whose source time predates the prior index watermark. The T1-candle close-only check also remains active during unresolved premium ordering.
- The full local suite on the final production code passed: **2,423 tests, 45 subtests, 12 warnings** in 176.51 seconds. Two additional duplicate-observation regressions were added after collection and passed in the 68-test guard-focused run above.
- Combined full and focused coverage exercised all **18 changed executable statements and 16 changed branch arcs** in `position_manager.py`.
- GitHub Actions run #599 passed for preceding commit `bc0c56f`, including database integration checks. This change requires a new run after push.

## Quote tie after a processed option bar

- Added CE and PE end-to-end regressions that first process a non-breaching 10:20 premium bar, then deliver a fresh ₹166 quote and a T2 index tick with the same source timestamp against the ₹167.20 moved cost stop.
- The tests reproduce the prior defect before the fix: the reference advanced to `T2_HIT` with no premium protective route. After the fix, the first stop trigger and `SPOT_OPTION_EXIT_TIE` remain recorded, the route intent is pending, the lifecycle remains at T1, and T2 allocation does not commit.
- The previously processed bar is not replayed or used to raise ATR again. Existing quote freshness/contract/activation/duplicate filtering and same-batch T2-plus-stop regressions pass.
- Full local suite: **2,427 passed, 45 subtests passed, 12 warnings** in 176.41 seconds. Coverage run exercised both event-time branches at the changed detector boundary; module-wide coverage is 90.5% statements / 86.9% branches, which is not a changed-code coverage claim.
- `git diff --check` passed. GitHub Actions run #600 passed on the preceding head `443c87e`; this fix needs its own CI run after push. No live service, broker fill, or production restart was tested.

## Route guard when creating T2-entry ambiguity

- The new CE/PE regressions use a completed post-T1 candle whose range touches both T2 and entry guard. Before the fix, the candle created `T2_ENTRY/ORDER_UNKNOWN` but emitted no route event; after the fix it retains a `SPOT_INDEX` route trigger and remains at T1 with the original 50/50 reference allocation and no `t2_at`.
- The normal dispatcher turns the intent into `EXIT_PENDING` for the remaining paper quantity. A later tick above entry and below T2 does not replace the first route. A reference-only control preserves the ambiguity without creating execution state.
- Focused guard and premium lifecycle modules: **134 passed, 2 warnings**. Coverage exercised every branch arc in the changed T2-entry creation/route block; this focused run is not whole-module coverage.
- Full local suite: **2,430 passed, 45 subtests passed, 12 warnings** in 177.91 seconds.
- Full-suite coverage exercised every branch arc in the changed T2-entry creation/route block. Module-wide `position_manager.py` coverage was 90.5% statements / 87.0% branches; no whole-module coverage claim is made.
- GitHub Actions run #601 passed on preceding head `cc6bb8a`; CI for this follow-up commit is required after push.

## Retained premium-trigger persistence identity

- CE and PE regressions first commit a non-breaching option bar, then submit a same-timestamp premium stop quote with an index T2 touch. They verify the trigger produces a distinct lifecycle ID and atomically persists the ambiguity, observed premium trigger, and execution-route-pending state.
- Restoring the committed snapshot through `OptionTrailingContext` retains the route and first trigger. Rewriting the unchanged trigger reuses its identity and adds no duplicate lifecycle commit.
- The new assertions failed against the previous event identity because no new ledger event was recorded; they pass with retained exit-trigger fields included in the identity.
- Focused premium lifecycle, commit-boundary, and paper-route modules: **151 passed, 2 warnings**. Full local suite: **2,437 passed, 45 subtests passed, 12 warnings** in 172.89 seconds. The final verification remains local until CI runs on the pushed commit.
- Focused branch coverage exercised both paths of the premium-only exit-identity condition in `position_manager.py`; the retry regression confirms the identity is stable for the same retained trigger.

## Shared-contract quote ordering

- A two-trade regression shares the pinned contract: the first trade accepts a ₹175 quote against its ₹167.20 cost stop while the second trade's ₹180 ATR stop becomes effective one second later. The quote is rejected for the second trade during ingestion; a tied T2 observation cannot turn it into `ATR_SL_HIT` or create a protective route for that trade.
- Same-close detection receives per-trade acceptance IDs and rechecks the current quote watermark, stop-effective time, pinned contract, and activation time before retaining an ambiguity or routing protection.
- Focused premium lifecycle, commit-boundary, and paper-route modules: **152 passed, 2 warnings**. The regression failed before the fix with a spurious `ATR_SL_HIT` for the second trade.
- Full local suite on the combined P1/P2 changes: **2,439 passed, 45 subtests passed, 12 warnings** in 173.80 seconds. `git diff --check` passed. The tests do not establish live broker or production behavior.

## Proven guard while creating T1-entry ambiguity

- The regression reproduces a confirmed PAPER runner with T1 at 10:20:30 and a completed 10:20 candle with high 24025, low 23995, and close 23999 against entry 24000. Before the fix, it created `T1_ENTRY/ORDER_UNKNOWN` without a route event.
- The close-based guard now persists a `SPOT_INDEX` route intent using the prior index watermark while keeping the reference trade at T1. The paper dispatcher places the remaining quantity in `EXIT_PENDING`; execution confirmation remains separate.
- Focused premium lifecycle, commit-boundary, and guard-route modules: **153 passed, 2 warnings**. The full local suite passed with **2,439 tests and 45 subtests**; these isolated tests do not establish live broker or production behavior.

## Restored terminal-route monitoring gate

- Three no-candle regressions cover protected candle-fetch, REST fallback, and tick polling with no active trades and only a restored terminal v2 execution-route record. Before the fix, none invoked the option-stop dispatcher.
- The gates now treat a non-empty terminal retry queue as monitoring work, allowing the existing dispatcher validation and paper-binding recovery to run without a new index candle.
- Focused lifecycle and routing suite: **223 passed, 2 warnings**. CI-scoped tests: **2,437 passed, 45 subtests passed, 12 warnings**. CI for this follow-up is pending push.

## REST monitor oi_fetcher guard and deterministic session time

- In CI, `test_restored_terminal_option_route_is_monitored_without_candle_or_active_trade[rest]` failed with `AttributeError: 'NoneType' object has no attribute 'get_nearest_expiry'` because `_session_now().time() < 15:30` reached chain fetch with `oi_fetcher=None`.
- `_monitor_rest_without_candle` now safely handles `oi_fetcher is None` and guards `engine.clear_cooldown()` against `None`.
- `test_restored_terminal_option_route_is_monitored_without_candle_or_active_trade` pins `_session_now` to 10:00 to verify no-candle terminal monitoring under intraday market conditions deterministically.
- Full test suite: **2,435 passed, 45 subtests passed, 12 warnings**.

## Scheduled flush throttle recheck timing isolation

- `test_scheduled_flush_rechecks_throttle_after_forced_write` previously exhibited runner-latency flakiness when elapsed time before scheduling exceeded 0.2s.
- Controlled monotonic clock via monkeypatching `quote_service.time.monotonic` ensures deterministic throttle entry, forced flush recheck, and completion.
- Suite verified locally: **37 passed in test_quote_integration.py**.
## Cost-stop-only T1 ambiguity persistence identity

- The regression processes a T1 tick and non-breaching ₹200 option bar at 10:20, then submits a fresh ₹165 quote sourced at 10:20 against the ₹161.05 initial stop and ₹167.20 entry cost.
- Without the ambiguity suffix, the ledger reuses the earlier bar event ID and creates no new row; the new ambiguity and quote watermark are absent from durable state. With the fix, the quote is marked `COST_STOP_ONLY_BREACH`, a distinct stable event commits the `T1_ORDER_UNKNOWN` snapshot, and an unchanged retry adds no duplicate row.
- The targeted regression passes. Full CI-scoped local suite: **2,439 passed, 45 subtests passed, 12 warnings** in 174.52 seconds. GitHub Actions run #607 passed for preceding head `36af219`; the cost-ambiguity identity fix requires its own exact-head CI run after push.
- No live provider, production restart, broker fill, or production record was used to validate this persistence regression.

## V2 milestone labels and break-even repair exclusion

- Fail-first regressions confirmed that v2 T2 exits through `ATR_SL_HIT`, `STOPPED_OUT_AT_BE`, and `OPTION_SL_FLOOR_HIT` returned a loss or no label with `t1_is_win=False`, and that cached normalization dropped v2 canonical outcome metadata.
- Shared milestone-policy handling now covers v1 and v2 in classification, joined dataset outcome selection, cached `trade_analytics` normalization, and terminal backfills. Legacy break-even repair now excludes both policies.
- Focused lifecycle, dataset, normalization, and repair checks: **104 passed, 2 warnings**.
- Full CI-scoped local suite: **2,446 passed, 45 subtests passed, 12 warnings** in 174.90 seconds on the synchronized branch.
- Exact-head GitHub Actions verification is pending push. No production data or broker execution was used.

## Guard routing on the initial T1 candle

- Fail-first CE and PE regressions reproduced the reviewer case: the reference advanced from PRE_T1 to T1, retained `T1_ENTRY/ORDER_UNKNOWN`, but emitted only `T1_HIT` while the confirmed paper remainder stayed `FILLED`.
- The initial T1 transition now routes the close-proven guard for an already-T1-booked confirmed PAPER execution, preserving the reference ambiguity and validating against the prior index watermark. The exact T1 boundary exception is limited to this transition.
- The complete guard-recovery module, including the new CE/PE first-T1 regression: **74 passed**.
- Full `tests/` suite: **2,448 passed, 45 subtests passed, 12 warnings** in 175.02 seconds. Compilation and `git diff --check` passed; exact-head GitHub Actions is pending push.
- No production data, live provider, or broker execution was used.

## Quote tied to an ATR raise

- A fail-first split-batch replay reproduced the report: after the ATR stop moved from ₹196.00 to ₹201.50, the same-time ₹195.00 quote emitted no exit event or route.
- The corrected regression covers same-batch and split-batch processing. Both close at the prior ₹196.00 ATR stop and route; the split-batch case also persists raised value ₹201.50 with `ATR_RAISE_TIED` metadata. A separate tied-raise case verifies a prior ₹167.20 cost stop remains `STOPPED_OUT_AT_BE` with zero runner points.
- The existing case where the quote breaches only the raised stop remains rejected. Focused lifecycle module: **68 passed**. Full `tests/` suite: **2,451 passed, 45 subtests passed, 12 warnings** in 176.82 seconds; compileall and `git diff --check` passed. Exact-head GitHub Actions evidence is pending push.
- No live provider, production record, broker order, or fill was used.

## T1 cost quote after ATR seed

- Fail-first replay reproduced the comment: after T1 seeded a ₹196.00 ATR stop, `previous_locked_option_sl` remained ₹161.05 and a fresh same-time ₹166.00 quote was rejected with no ambiguity.
- `_seed_option_stop` now stores the moved ₹167.20 cost stop as the previous effective stop before applying the ATR candidate. The quote is retained as `T1_ORDER_UNKNOWN/COST_STOP_ONLY_BREACH`; it does not emit a premature execution route.
- Focused option-premium lifecycle module: **69 passed**. Full `tests/` suite: **2,454 passed, 45 subtests passed, 12 warnings** in 175.94 seconds. Compilation and `git diff --check` passed; exact-head GitHub Actions is pending push.
- No production data, live provider, broker order, or fill was used.

## P1: quote tied to a committed T2 across monitor batches

- Fail-first split-batch replay reproduced the comment: a PRE_T1 index tick reached T2 at 09:20, then a fresh ₹160 premium quote with source time 09:20 and receipt time 09:20:02 returned no `SL_HIT`, ambiguity, or execution route. The same observations in one batch retained and routed the original-stop breach.
- The new path persists `T2_ORDER_UNKNOWN` with the protective stop trigger, quote/source/receipt times, possible full/half/runner allocations, and a paper exit intent. It leaves the already committed T2 reference milestone untouched. The exception runs before an equal-time quote watermark, keeps a previously active break-even/ATR stop authoritative when T1 predates T2, and honors the previous stop when an ATR raise shares the timestamp. A later ₹180 quote does not replace the retained trigger; other delayed/out-of-order quotes remain rejected.
- Focused option-premium lifecycle module: **74 passed, 2 warnings**. Regressions restore the committed `OptionTrailingContext`, exercise an equal-time preexisting quote watermark, verify break-even status/zero runner points after earlier T1, and ensure a simultaneous ATR raise preserves prior-stop protection and ordering metadata. Full `tests/` suite: **2,457 passed, 45 subtests passed, 12 warnings** in 174.15 seconds.
- `python -m compileall -q position_manager.py` and `git diff --check` pass. Exact-head GitHub Actions is pending push. No production data, live provider, broker order, fill, deployment, or worker start was used.
