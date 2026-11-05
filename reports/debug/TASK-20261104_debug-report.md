# Debug Report — PR #156 Terminal Paper Exit Recovery

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

A quote in a later batch with the same source timestamp as a committed ATR raise was compared with the newly raised stop, even though its order relative to the raise was unknown. The quote is now rejected with quote monitoring marked unavailable. A later fresh quote uses the retained stop. The tied quote is not used to infer an exit at the prior stop either; no earlier exit is established without further evidence.

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
