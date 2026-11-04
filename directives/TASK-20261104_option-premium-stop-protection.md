# TASK-20261104: Option-Premium Stop Protection and Post-T1 ATR Precedence

**Date:** 2026-11-04
**Status:** Implemented and verified locally; production rollout not authorized
**Branch:** `feature/TASK-20261104-option-premium-stop-protection`

## Goal

Protect option-tracked trades with stops in the selected option's premium domain. Before T1, enforce the original premium stop. At T1, move the remaining position or reference runner's premium stop to its authoritative entry cost. Thereafter, use an option-premium ATR trailing stop only when its derived stop price is above the protected stop; ATR may tighten protection but can never weaken it. Enforce the active premium stop on fresh valid option-price observations, independently of the ATR candle calculation cadence.

## Verified Incident Context

Read-only records for 4 November 2026 establish:

- **#2455** is a bullish `FAILED_BREAKOUT` reference signal for NIFTY 23950 CE, security ID `39961`, expiry 2026-11-09. Its configured premium entry reference is ₹167.20 and original option SL is ₹161.05. T1 was recorded at 09:27:40.987 IST. The persisted stop was the spot entry ₹23,908.20; `option_trailing` was null.
- **#4176** is a bullish `TREND_CONTINUATION` reference signal for the same contract. Its configured premium entry reference is ₹159.35 and original option SL is ₹143.35. T1 was recorded at 10:06:14.389 IST. Its persisted stop was the spot entry ₹23,909.90; `option_trailing` was null.
- Both records have `tracking_mode=SIGNAL_REFERENCE`, zero lots, and `NO_AVAILABLE_CAPITAL`. Both closed with `ATR_SL_HIT` at 10:20 IST through the legacy spot-ATR context (`atr=38.5037`, five-minute watermark 10:20, stop equal to spot entry). Approximate option reference exit marks were ₹134.05 at 10:21. No broker stop orders or trade-exit rows were recorded.
- Durable storage contains no intermediate premium candle or quote series for these trades. The records establish the routing and terminal event, but do not establish the earliest premium stop-breach time. The reported 10:35–10:40 and 13:32 times are not supported by these two records.

Current source also gates `option_trailing` enrollment on option-ATR configuration, approved version, and backend readiness. Trades without that context continue through the legacy spot-ATR monitor. Even when option ATR is enrolled, the current candidate ratchet uses the original option SL as its floor and the monitor exits only when a completed five-minute close is strictly below the active stop. These behaviors do not enforce the requested entry-premium floor or an intervening premium stop breach.

## Scope

- Trace and update option-tracked trade enrollment, premium stop lifecycle, T1 activation, ATR updates, price monitoring, terminal persistence, restart recovery, and alerts.
- Keep the currently configured option ATR formula and timeframe unless the Architect verifies a concrete source/config discrepancy. The current code specifies five-minute option candles, Wilder ATR(5), HHV lookback 10 of `high - 2.5 * ATR`, and completed-candle ATR updates. The supplied one-minute chart does not authorize changing that timeframe.
- Use the selected contract identity throughout: security ID, exchange segment, expiry, strike, and CE/PE.
- Treat bought CE and bought PE equally as long-premium positions.
- Prevent the legacy spot-ATR path from silently managing an option-tracked trade. Missing option data must retain the last protected premium stop and report monitoring unavailability; it must not substitute spot ATR.
- Preserve signal selection, sizing, and unrelated lifecycle behavior. Keep T2 as a reference milestone and preserve existing model/reference booking, partial-booking fractions, scoring, and accounting unchanged. Do not add executable allocation, broker execution, or assert an actual partial fill for T2; do not introduce a new T2-driven premium-stop change.
- Distinguish confirmed fills from reference prices. Do not alter historical or closed production rows.

## Stop and Exit Rules

1. Before T1, the original option-premium SL is the active premium stop. ATR cannot change the stop or trigger an ATR exit before T1.
2. On T1, atomically activate the post-T1 phase and raise the premium stop to at least the authoritative entry cost, preserving any already tighter premium stop.
3. After T1, for bought CE or PE:

   ```text
   effective_stop = max(previous_effective_stop, entry_premium, valid_atr_stop_candidate)
   ```

   Omit an absent or invalid candidate. A candidate at or below the effective stop cannot lower or replace it. The effective stop never decreases across updates, data refresh, or restart.
4. Compute ATR candidates only from valid completed option-premium candles under the verified configured policy. A newly computed candidate becomes effective from its own valid completion time; never apply it retroactively.
5. Check the active premium stop on each valid, fresh, contract-matched option-price update. A price equal to or below the stop triggers the exit path immediately, including gaps and breaches between ATR candle closes. Do not wait for a spot confirmation or the next ATR candle.
6. Classify a hit of the moved entry-cost premium stop as `STOPPED_OUT_AT_BE` and report `0.00` runner stop-out points, including when the quote gaps below cost. Preserve the observed/reference exit mark separately; do not fabricate a fill at cost or apply an unrequested fee adjustment. Classify a hit after ATR has tightened the premium stop above cost as `ATR_SL_HIT`. Preserve actual broker fills separately from trigger levels and reference/model exit marks.
7. Maintain chronological event ordering around T1 and stop breaches. OHLC candles cannot establish exact intrabar ordering when T1 and a stop touch share a candle; report bounds or unresolved order rather than choosing the desired event.
8. Reject wrong-contract, malformed, stale, out-of-order, and future-dated observations under the verified feed contract. Missing ATR history, warmup, or disabled ATR calculation must not remove or weaken an already active stop.

## Acceptance Criteria

- An option-tracked trade cannot be enrolled without a complete pinned contract and sourced premium entry/SL values.
- A pre-T1 premium touch of the original option SL closes through the selected trade's terminal/reference accounting path and cannot invoke ATR. The ADR must define its reason label without reusing the post-T1 break-even reason.
- T1 immediately and durably protects the remaining position/reference runner at entry cost.
- ATR candidates below, equal to, or missing against break-even leave the entry-cost stop active.
- A premium quote touching/crossing break-even exits as `STOPPED_OUT_AT_BE`, with zero modeled premium points at exact cost, even between five-minute candle closes.
- A post-T1 independent spot/index break-even guard closure also routes a matching v2 paper execution record to an exit intent for every remaining unit; it stays pending until an executable premium bid confirms the paper exit.
- A pre-T1 spot/index initial-stop closure routes to the same execution exit path if paper execution has already booked its independent premium T1; all remaining units stay pending until an executable premium bid confirms the exit.
- Quote receipt freshness is anchored to the quote batch fetch, so internal option-history latency cannot discard a quote that was fresh when fetched; accepted quotes retain source-time replay ordering.
- If a post-T1 premium stop triggers while an earlier meaningful index touch is queued behind incomplete option history, route the protective execution intent immediately but keep the reference lifecycle active. Replay the queued index observation and recovered option history in source-time order before finalizing the premium exit or its reference allocation.
- During that recovery, replay completed option bars strictly before the retained premium-trigger time. An earlier bar close through the then-active premium stop wins over a later queued index milestone; do not process bars at or after the trigger as earlier evidence. Preserve the already-routed execution intent.
- If an accepted v2 premium quote or completed option bar breaches the stop at the same source timestamp as an index T2/entry-guard touch, retain the reference ordering ambiguity and emit the premium protective event/intent immediately, before milestone processing can commit T2. Continue rejecting genuinely delayed quotes at or before an already committed `t2_at`.
- If a premium quote shares T1's source timestamp and is at or below the original premium SL, preserve the T1/reference-allocation ambiguity but retain the stop trigger and route protective execution immediately; the advanced quote watermark must not erase the breach.
- If an ATR-enabled v2 completed option bar ends at T1 and closes at or below the original premium SL, persist the T1 ordering ambiguity across restart and route protection immediately, even when history arrives after T1. Do not assume a full pre-T1 or half post-T1 reference exit; same-time quotes, later bars, or later index milestones cannot replace or finalize the first retained trigger. Preserve subsequent T2/break-even guard touches for reconciliation.
- Repeated premium triggers and deferred protective-exit finalization preserve `REFERENCE_TRIGGERED_NO_BROKER_FILL` for reference-only trades; do not create paper execution-route state without an execution-backed fill.
- Reject same-close protective ambiguity when the index/option event predates the trade activation/fill time or is stale behind its applicable event watermark. On an exact-T1 tie for a PRE_T1 trade, retain a quote breach at/below the original premium SL before index milestones; if the same index event also reaches T2, keep the reference at T1 and retain the tied index observation until ordering is reconciled.
- For an ATR-enabled T1-tied option bar closing above the original SL but at/below entry cost, persist order-unknown and mark the bar processed instead of applying the post-T1 cost stop retroactively. Keep immediate protection when the close also breaches the original SL.
- Keep later index guard/T2 observations on unresolved cost-stop-only T1 ambiguities. Route a later premium stop through a separate protective execution intent while deferring reference finalization until the earlier ordering is resolved.
- While that T1-tied protective trigger remains unresolved, later quotes or ATR bars cannot finalize the reference exit or replace its original timestamp, price, stop, or allocation.
- When certified ordered ticks resolve a v2 T1/T2-entry ambiguity to a post-T1 index break-even exit, record `stop_domain=SPOT_INDEX` and persist the execution-route-pending marker for confirmed paper fills so the common monitor routes the remaining units.
- Certified ordered spot/index ticks establish index-touch order only. If a T1-tied premium quote or bar remains unresolved, keep the index ambiguity pending and do not finalize a guard/T2 outcome or commit an assumed exit allocation from those ticks.
- Treat a completed v2 option close equal to the active stop as a breach in the same-close ambiguity detector, matching normal premium-stop evaluation so an equal-time index milestone cannot finalize first.
- Persist execution-route retry state for filled v2 trades. Isolate dispatcher failures per trade, continue routing other stopped positions, and retry an unacknowledged route on later monitor passes, including after the trade is terminal. Clear the marker only after both route synchronization and a terminal-safe metadata acknowledgement persist; acknowledgement must not submit a lifecycle transition or change its revision.
- Once T2 is committed, reject an option quote whose source timestamp is at or before `t2_at` as out of order. It must not be evaluated using the POST_T2 remaining fraction; expose the quote-monitoring gap and retain the protected stop.
- An ATR candidate above the protected stop tightens protection; a later falling candidate cannot loosen it. A subsequent breach exits as `ATR_SL_HIT`.
- Bought CE and PE have identical long-premium stop comparisons.
- Wrong-contract or invalid-time data cannot alter stop state; restart restores phase, effective stop, contract identity, and processed-event/candle watermarks.
- An expired or malformed restored contract is skipped individually during protective quote-batch construction; it must not block quote requests for other valid active contracts. If no valid pins remain, make no provider request.
- When contemporaneous spot evidence is available for a premium-triggered exit, persist that spot mark as the lifecycle `exit_price` in both accounting ledgers; leave it null when no evidence is available.
- Duplicate or concurrent events cannot duplicate terminal closure or reopen a closed trade.
- A gap through a stop records the stop trigger separately from observed/reference price and actual fill.
- T1/stop source chronology is respected without lookahead; unresolved candle ordering remains explicitly unknown.
- If a premium stop triggers while an earlier index T2/entry-guard candle is unresolved, retain the first premium trigger and route its execution intent, but keep the reference lifecycle open and ambiguous until certified ordered evidence establishes which event came first. Finalize milestone booking, exit fraction, stop status, and reference accounting only after that ordering is resolved.
- For v2 ATR-enabled trades, track premium-quote and ATR-history monitoring health independently. Healthy ATR bars must not clear a quote outage, and quote recovery must not hide an ATR-history outage; report monitoring restored only after every required feed has recovered.
- Option-tracked trades cannot silently fall back to the spot-ATR monitor when ATR is disabled, unavailable, or unapproved.
- Tests and local lifecycle/replay/persistence checks exercise the actual application path with external order execution disabled. No test, replay, or screenshot is represented as a confirmed broker fill.
- For #2455 and #4176, report the verified old-path timeline and the limits caused by absent premium history; do not manufacture a corrected historical exit time.

## Out of Scope and Release Boundary

- No production configuration change, ATR activation/parity sign-off, migration of existing or closed rows, deployment, service restart, worker start, broker order, or Discord test message.
- No historical production correction or inference of a broker position, fill, or realized loss from the zero-lot reference records.
- No change to signal/target generation, sizing, existing T2 model/reference booking or scoring, partial-booking behavior, or unrelated accounting.
- Any schema, backend capability, configuration, or rollout requirement discovered during architecture must be documented separately. Production activation and existing-trade migration require their own explicit authorization.

## Required Next Phase

The Software Architect must inspect the current option-price feed, existing lifecycle transaction/RPC invariants, and feature gates; define the authoritative premium entry for confirmed-fill and `SIGNAL_REFERENCE` modes; specify the minimum persistence changes; and resolve any conflict with the prior option-ATR ADR. Present the ADR for human approval before implementation.
