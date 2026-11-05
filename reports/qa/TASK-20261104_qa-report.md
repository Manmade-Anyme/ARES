# QA Report — PR #156 Terminal Paper Exit Recovery

**Verdict:** PASS — terminal-route regressions and the CI-scoped test suite pass; latest-head GitHub Actions is pending push.

## Verification

| Check | Result | Evidence |
|---|---|---|
| Focused lifecycle and routing tests | PASS | 223 passed, 2 warnings in 70.98s |
| CI test scope (`tests/`) | PASS | 2,437 passed, 45 subtests passed, 12 warnings in 174.65s |
| New monitoring-gate statements | PASS | The terminal-route helper and protected-fetch, REST, and tick-poll gates were exercised by the focused coverage run |
| New monitoring-gate branches | PASS | All three route-queue paths reached the existing dispatcher with no active trades or new candle |
| Python compilation | PASS | `python -m compileall -q position_manager.py execution/signal_dispatch.py main.py` |
| Whitespace validation | PASS | `git diff --check` |
| Latest-head GitHub Actions | PENDING | Push is required before CI can run against this revision |

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
