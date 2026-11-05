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
