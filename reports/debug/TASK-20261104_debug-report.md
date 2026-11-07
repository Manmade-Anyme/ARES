# Debug Report — PR #156 Terminal Paper Exit Recovery

## P2: route pinned depth bids to pending paper exits

The protective quote batch already fetched `top_bid_price`, but discarded the depth and sent only last price to the reference lifecycle. When the option chain was unavailable, a premium stop could create a paper `EXIT_PENDING` order without any path to `OrderManager.observe_quote`'s bid-based confirmation. Pinned batch and selected-option quote observations now retain validated bid/ask depth and pass them to the dispatcher after lifecycle stop routing. The dispatcher checks freshness, positive finite bid, non-crossed depth, and the complete pinned contract; only confirmed PAPER orders with a pending exit can complete. Terminal route queue entries remain quote candidates after restart. Regressions cover bid propagation, crossed/stale/wrong-contract rejection, pending exit completion at the observed bid, and terminal-route polling.

## P2: quote source second overlaps fractional fill time

A Dhan last-trade quote at 10:20:00 with a ₹160 premium was dropped when the paper fill occurred at 10:20:00.500. Since Dhan reports whole seconds, the trade may have occurred after fill and before recovery. The stop breach is now retained as `ENTRY_ORDER_UNKNOWN/FILL_SECOND_OVERLAP` when receipt follows fill, with no premature execution route. Fully pre-fill seconds remain rejected. A recovered quote and later T1 do not erase or finalize the ambiguity.

## P2: T1-tied quote against a newly seeded ATR stop

Fail-first CE/PE replay seeded a ₹196 ATR stop at T1, then rejected a fresh ₹190 quote from the same source second. If the quote followed T1, it should leave reference exit ordering unresolved and prevent later T2 from being treated as certain. The targeted guard retains `T1_ORDER_UNKNOWN/ATR_STOP_ONLY_BREACH`, the seeded level, and quote provenance without creating an exit route.

## P1: stale writer erased committed protective intent

Four fail-first immediate/outage and T2/terminal replays erased the committed SPOT_OPTION_EXIT_TIE, first trigger, and pending route. Recovery now rebases stale transitions to the committed unresolved allocation and intent, retains later observations, and suppresses stale milestone/exit alerts. Additional cases cover deferred exits, queued stronger stops, serialization, and a later proven breach while committed cost-only uncertainty has no route.

## P2: quote-watermark regression during revision repair

Three fail-first milestone, terminal, and outage/newer-local-quote replays violated the SQL monotonic quote-watermark invariant, leaving writes queued. Recovery now rebases queued snapshots to the committed watermark, advances live state only when newer, and refreshes alerts after rebasing. Quote/exit observation provenance remains intact.

## P2: partial stop metadata during revision repair

Fail-first replays used a local ₹167.20 cost stop and a committed ₹180 ATR stop. Recovery copied the higher stop but retained `BREAK_EVEN_COST`, violating the v2 SQL source/price invariant and leaving the write queued while the database stayed at T1. Four milestone/terminal and immediate/outage-retry variants reproduced the failure. Both adoption branches now copy the full stop revision into queued and eligible live/alert state. Tests validate queue drainage, serialization, subsequent ATR protection, and immutable observed break-even exit provenance.

## P2: T1-tied bar below a newly seeded ATR stop

Fail-first CE/PE replays seeded ₹196 ATR at a 10:30 T1 tick, then closed on the simultaneous ₹190 option bar despite original SL ₹161.05 and entry cost ₹167.20. Same-batch and split-batch cases all emitted an unwarranted ATR exit. The T1 ambiguity helper now recognizes this seeded-stop-only range, stores `ATR_STOP_ONLY_BREACH` and the seeded level under a distinct stable identity, and defers reference finalization. Regression coverage verifies persistence/retry, recovery without erasure, and a later genuine ATR breach routing protection separately.

## P2: premium quote accepted before its same-time index milestone

Fail-first replay accepted ₹165 at 09:20 against initial SL ₹161.05 and entry cost ₹167.20, then received a same-source-time T2 tick two seconds later. All four arrival/restart variants committed T2 without cost uncertainty. Accepted cost-only observations now persist by source second and are reconciled before milestones, so T1 is recorded while T2 remains deferred behind `COST_STOP_ONLY_BREACH`. The cache survives an intervening quote and serialization; legacy snapshots reuse matching last-quote evidence. Reconciliation validates contract/receipt time, preserves newer market evidence, and clears pending quotes in the T1 snapshot.

## P2: changed prices inside a watermarked source second

Fail-first CE/PE replays accepted ₹180 then discarded a changed stop-breaching price at the same source second for initial, break-even, and ATR stops. The ingestion and downstream guards treated every equal timestamp as a replay. A shared timestamp/price replay check now admits changed prices while keeping existing chronology validation. Quote identity includes source/receipt time and price so revisions (including a return to an earlier price) persist without revision rollback. Added regressions preserve T1 ambiguity through recovery and prove both same-batch price orders avoid post-terminal writes.

## P2: delayed cost-only quote tied to committed T1/T2

Fail-first split-batch replay committed a PRE_T1 tick through T2, then lost a ₹165 quote sourced at that tick against initial SL ₹161.05 and entry cost ₹167.20. The T2 handler now reuses T1 cost-only ambiguity retention, marks T2 as already committed with disputed allocation, and preserves a newer watermark when present. It creates no protective exit/route from the cost-only observation. Regressions verify durable/recovered ambiguity, idempotent retries, and a later genuine premium breach routing protection without finalizing the unresolved reference exit.

## P2: delayed T2 quote moved the watermark backward

Fail-first replay committed T2 at 09:20:00, then a non-breaching ₹180 quote at 09:20:01, then received a protective ₹160 quote sourced at 09:20:00. The retention helper regressed `quote_watermark`, violating the lifecycle RPC's monotonic rule. It now advances that watermark only for a newer source time while preserving the earlier protective event time. The regression enforces the database rule in its ledger and checks committed ambiguity/route state, an empty pending-write queue, and serialization recovery.

## P1: T2-tied quote predating a later ATR raise

Fail-first replay processed T2 at 10:29:59 with a ₹196 stop, then a 10:30 completed option bar raising the stop to ₹201.50, then a ₹198 quote sourced at 10:29:59 and received at 10:30:02. Both ordinary ingestion and the equal-time quote-watermark exception incorrectly emitted `ATR_SL_HIT` and marked an execution route. The shared retention helper now rejects a quote that strictly predates the locked stop's effective time. The exact-raise-time prior-stop comparison remains intact.

**Verdict:** CLEAN — the P1 restart gap is addressed in the branch; live service and broker behavior were not exercised.

## Finding and cause

The option execution retry monitor previously considered active trades only. After restart, a reference trade that had become terminal could therefore lose its in-memory route intent and paper-order binding. An accepted route marker also did not by itself keep a paper execution in `EXIT_PENDING` connected to quote monitoring, so an exit awaiting observed bid confirmation could be stranded.

## Fix

- Restore pending terminal route records from persisted analytics context and retry their terminal-safe route acknowledgement.
- Persist a complete paper `OrderRecord` snapshot with execution telemetry, and restore the signal/order binding only after validating signal identity, PAPER mode, confirmed fill, status, expiry, strike, security ID, exchange, and option type. A validated legacy snapshot fallback remains supported.
- Keep terminal paper executions with `EXIT_PENDING` on the routing and quote-monitor path after acknowledgement. Clear the route queue only when acknowledgement succeeds and no pending paper exit remains.
- Fail closed on malformed or mismatched state; no live execution is restored from this recovery path.

## Evidence boundary

The restart regression tests cover restored legacy and full snapshots, route acknowledgement, resumed bid processing, and malformed or unavailable persisted state. Local tests do not prove a production restart, database RPC behavior under live service conditions, broker fills, or trading outcome.

## Follow-up review findings

- Premium triggers advance the lifecycle execution status to `EXIT_TRIGGERED_PENDING_CONFIRMATION` before the paper exit fills. Both recovery paths now accept that status while retaining paper-mode, protected-fill, quantity, and contract checks. Recovery tests exercise both the legacy and complete snapshots, plus invalid and reference-only inputs.
- Restored exit-only bindings do not have the full entry signal used by pending-entry deduplication. Marking and excluding them from that check lets a new valid entry dispatch while the recovered exit stays pending. Ordinary pending-entry deduplication is still covered.
- A recovered earlier option stop or index guard can supersede a deferred later premium trigger. Clear the superseded metadata before constructing the terminal lifecycle snapshot. The deferred-event handler no longer submits another transition after closure. A terminal-guard ledger reproduces the database rejection and exercises lost-response retry and nonclosing recovery.

## Index guard during an option-history outage

An eligible index-entry guard was queued for reference reconciliation while the paper execution could remain open. The monitor now retains a durable, contract-checked index execution intent and routes it while reference ordering remains pending. It preserves an existing protective intent, expands a pending T1 partial to all remaining units, and restores an acknowledged pending exit after restart. A T1-spanning candle cannot use its potentially pre-T1 wick as proof of a later guard touch; its close can establish the touch. A distinct retry-stable identity prevents an earlier watermark at the same source time from suppressing the new intent. Malformed route metadata is isolated from other trades.

## Quote tied to an ATR raise

An earlier ordering guard rejected every quote in a later batch with the same timestamp as a committed ATR raise. The new P1 replay found that this also discarded quotes already below the prior stop, even though that breach is protective under either ordering. The correction below supersedes that behavior: prior-stop breaches route at the prior stop, while quotes that breach only the raised stop remain rejected as order-unknown.

## T1-candle timeline replay

Timeline replay supplies completed candles with an explicit event time, which bypassed the T1-candle close-only execution check. The safeguard now applies whenever a completed candle starts at or before T1, including replay; its full range remains available for reference-order reconciliation. CE and PE regressions cover a wick-only touch and a close that actually reaches the guard.

## Index guard during unresolved reference ordering

A cost-stop-only T1 ambiguity kept later index touches for reference reconciliation but bypassed the execution guard when option ATR history was disabled or available. A proven guard touch now creates the same validated, durable paper execution intent while preserving the unresolved reference ambiguity. Existing protective intents retain priority; stale, pre-T1, and reference-only observations cannot create a new route.

## Fresh quote tied to an already processed option bar

The POST_T1 same-close detector used `last_processed_option_bar_end` to skip the whole event. A later fresh quote at the same timestamp as an already processed non-breaching bar was therefore skipped along with the bar; the following index T2 tick could commit its milestone before the quote path rejected the quote against the new T2 state. The detector now lets the bar watermark filter bar evidence only and still evaluates accepted quote evidence. The quote's existing freshness, pinned-contract, stop-effective-time, and quote-watermark checks remain unchanged. CE and PE replay tests verify a same-time ₹166 quote against the ₹167.20 cost stop retains order ambiguity and routes paper protection before T2 commits. Local tests do not establish production runtime behavior or broker fills.

## Route protection when creating a T2-entry ambiguity

When a completed post-T1 index candle first touched both T2 and the entry guard, the code persisted `T2_ENTRY/ORDER_UNKNOWN` and returned before retaining a paper execution route. A later tick above entry but below T2 could not recreate that missed guard. The creation branch now calls the existing validated route helper with the pre-update index watermark, appends a route event only for an eligible confirmed PAPER execution, and persists the new ambiguity and route intent together. It leaves the lifecycle at T1 and does not commit an assumed allocation or resolve event ordering. CE/PE replay tests verify the runner exits through the dispatcher while the reference ambiguity remains; a reference-only control does not route.

## Retained premium-trigger persistence identity

A fresh quote tied to an already processed option bar correctly created an in-memory ambiguity and pending route, but its lifecycle ID was derived only from the unchanged stop and bar watermark. The persistence RPC therefore treated the new trigger as an idempotent replay and kept the previous snapshot. Lifecycle identity now includes the immutable premium exit trigger fields, so the ambiguity and route marker commit under a distinct ID; an unchanged retry reuses that ID. CE/PE persistence regressions verify the committed snapshot survives `OptionTrailingContext` restoration and that retries do not add a second commit.

## Shared-contract quote ordering

Quote ingestion accepted a shared-contract quote for one trade but rejected it for another because the second trade's ATR stop became effective after the quote timestamp. The same-close detector discarded that per-trade distinction and could apply the quote to both. The quote batch now retains the IDs of trades that passed ingestion, and the detector rechecks each accepted trade's current quote watermark, active-stop time, contract, and activation boundary before using the quote. A two-trade replay reproduces the stale quote rejection and confirms it cannot close the ATR-raised trade.

## Guard routing while creating T1-entry ambiguity

A completed candle spanning T1 can use its close to prove the entry guard was touched after T1, while its wick leaves T1-versus-guard ordering unknown. The `T1_ENTRY` branch retained that ambiguity but returned without routing the proven paper guard. It now records the validated guard intent using the prior index watermark in the same persisted transition. The lifecycle stays at T1; the execution dispatcher receives the remaining-unit exit. The regression verifies the route becomes `EXIT_PENDING` and the reference ambiguity remains.

## Restored terminal-route monitoring gate

After restart, valid pending execution routes are held in `_option_execution_route_trades`, separate from `active_trades`. The no-candle REST fallback, protected candle-fetch, and tick-poll gates previously checked only active option-policy trades, so they skipped `_monitor_completed_atr` when only terminal retry work remained. Those gates now also recognize the terminal route queue and invoke the existing validated dispatcher path. A no-candle replay through each gate confirms the pending paper exit is retried without requiring an active trade or a new index candle.

## REST monitor optional oi_fetcher and session time isolation

During CI execution in UTC before 15:30, `_monitor_rest_without_candle` proceeded into option chain retrieval. When invoked with `oi_fetcher=None` (as in isolated unit tests of the terminal route monitoring gate), `await oi_fetcher.get_nearest_expiry()` raised an unhandled `AttributeError`. The REST monitoring gate now checks whether `oi_fetcher` is provided; if absent, it schedules the fallback selected contract quote if available and returns safely. Furthermore, `test_restored_terminal_option_route_is_monitored_without_candle_or_active_trade` now explicitly pins `_session_now` to intraday market hours so its coverage remains deterministic regardless of the wall-clock time of test execution.

## Scheduled flush throttle recheck timing isolation

In CI, `test_scheduled_flush_rechecks_throttle_after_forced_write` intermittent failure occurred when runner latency between forced flush and flush scheduling exceeded the 0.2s throttle window, resulting in `delay=0` and bypassing `controlled_sleep` so `wait_started.wait()` timed out. The test now controls `quote_service.time.monotonic` deterministically, guaranteeing the throttle wait is entered and subsequent throttle recheck completes without relying on wall-clock timing.
## Cost-stop-only T1 ambiguity persistence identity

The lifecycle event ID used the trade, update kind, source timestamp, active stop, and processed-bar watermark. A fresh T1-tied quote between the original SL and entry cost could therefore reuse the ID of an earlier non-breaching bar at that timestamp. The database correctly treated it as an idempotent retry, leaving the new `T1_ORDER_UNKNOWN` ambiguity and quote watermark only in memory. The quote branch now labels this case `COST_STOP_ONLY_BREACH`, and lifecycle identity includes the stable ambiguity fields for cost-only T1 quotes/bars. This commits the uncertainty without claiming that T1 or the stop came first.

## V2 outcome labeling and legacy break-even repair

The v2 policy was absent from the milestone-aware labeling checks, so a T2 trade closed by `ATR_SL_HIT` or `STOPPED_OUT_AT_BE` fell through to legacy exit-reason labeling and could be classified as a loss when `t1_is_win=False`. The joined dataset and cached analytics normalizer also did not consistently prefer the canonical v2 `result_state` and `option_trailing` metadata. Shared policy membership now covers v1 and v2 in those paths and terminal repair; the legacy target-based break-even repair excludes v2 to preserve canonical zero-P&L accounting.

## Guard routing on the initial T1 candle

When the paper execution had already booked T1 but the reference lifecycle was still PRE_T1, the first index candle could advance the reference to T1 and close through entry. The code retained `T1_ENTRY/ORDER_UNKNOWN` but the protective route helper was only called from post-T1 branches, so the confirmed paper runner remained open. The initial T1 transition now uses the completed close as guard evidence, preserves the ambiguity, and routes the remaining units using the prior index watermark. The helper permits the exact T1 boundary only for this already-booked paper-T1 case.

## P1: preserve prior-stop protection for a quote tied to an ATR raise

With the active stop at ₹196.00, a completed option bar raised it to ₹201.50, and a fresh ₹195.00 quote sharing that bar timestamp, processing both together produced `ATR_SL_HIT` and a pending execution route. When the quote arrived in a later monitor batch, the timestamp guard rejected it without checking that it had already breached ₹196.00. That lost a protective event valid under either source ordering.

The quote-time guard now permits a tied quote at or below `previous_locked_option_sl`; closure uses that prior stop and source. The trigger persists `stop_ordering_status=ATR_RAISE_TIED` and the raised value. Quotes between the old and raised stop remain rejected because their trigger depends on event order. A prior break-even stop retains `STOPPED_OUT_AT_BE` and zero runner points.

## P2: retain the T1 cost-only quote when ATR seeds at T1

Fail-first replay used a T1 event at 10:26 with cached option bars through 10:25. `_seed_option_stop` raised the moved cost stop from ₹167.20 to ₹196.00 but left `previous_locked_option_sl` at the original ₹161.05 stop. A fresh ₹166 quote sourced at 10:26 and received at 10:26:02 was therefore rejected as an ATR-raise tie before the T1 ambiguity handler; no `COST_STOP_ONLY_BREACH` state was retained.

The seed now records the actual pre-raise stop (₹167.20) before installing the ATR stop. The split-batch quote passes the stop-time guard and becomes `T1_ORDER_UNKNOWN/COST_STOP_ONLY_BREACH` without creating an execution route. This leaves the reference ordering unresolved rather than losing a potential moved-cost stop event.

## P1: preserve a premium stop quote tied to T2 across monitor batches

Fail-first chronological replay processed a PRE_T1 index tick that reached T2 at 09:20, then delivered a fresh ₹160 option quote in the next batch with source time 09:20 and receipt time 09:20:02. The index event committed T1 and T2. The post-T2 time guard then rejected the quote before comparing it with the original ₹161.05 premium stop, leaving no quote ambiguity or paper execution-route marker. The same observations in one batch retained and routed `SL_HIT`.

The exact-T2-time exception now retains a quote as `T2_ORDER_UNKNOWN` when it breaches the stop protective under every possible ordering: the original premium stop for a T1 tie/unknown, or the active break-even/ATR stop when T1 predates T2. A quote tied to an ATR raise at that same timestamp must also breach the previous effective stop. Check the protective quote before an equal-time quote watermark can discard it, preserve the correct stop reason and possible full/half/runner allocations, and route confirmed paper protection without changing committed T2. A later premium recovery cannot replace the trigger. Regression coverage checks split-batch and pre-watermarked cases, prior-T1 break-even status, same-time ATR raise protection, and the existing same-batch case.

## P2: require ML-label privileges for v2 readiness

The v2 capability RPC checked lifecycle table access and the label-sync function's execute privilege, but did not check `public.ml_collection` table access. A service role missing `SELECT` or `UPDATE` could therefore enroll a trade and later have a terminal lifecycle transaction roll back when label synchronization failed. The capability check now requires both privileges; the isolated PostgreSQL regression revokes each one separately and expects readiness to fail closed. The repository suite passed (2,507 tests and 45 subtests), and exact-head Actions run #632 passed on `bcde770`, including both PostgreSQL lifecycle checks. That CI head predates the depth-bid follow-up.
