# QA Report — PR #156 Terminal Paper Exit Recovery

**Verdict:** PASS — local regression suite and changed-code coverage pass; latest-head GitHub Actions is pending push.

## Verification

| Check | Result | Evidence |
|---|---|---|
| Focused lifecycle and routing tests | PASS | 142 passed, 2 warnings |
| Full repository suite | PASS | 2,340 passed, 45 subtests passed, 12 warnings in 173.41s |
| Changed executable statements | PASS | 142/142 covered across `execution/signal_dispatch.py`, `main.py`, and `position_manager.py` |
| Changed branch arcs | PASS | 56/56 covered across the same files |
| Python compilation | PASS | `python -m compileall -q position_manager.py execution/signal_dispatch.py main.py` |
| Whitespace validation | PASS | `git diff --check` |
| Latest-head GitHub Actions | PENDING | Push is required before CI can run against this revision |

The test run emitted 12 existing dependency/runtime warnings. No live service restart, broker interaction, or production trade was performed. The review thread remains for human review; no approval or merge is implied by this report.

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
