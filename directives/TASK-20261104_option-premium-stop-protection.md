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
- An ATR candidate above the protected stop tightens protection; a later falling candidate cannot loosen it. A subsequent breach exits as `ATR_SL_HIT`.
- Bought CE and PE have identical long-premium stop comparisons.
- Wrong-contract or invalid-time data cannot alter stop state; restart restores phase, effective stop, contract identity, and processed-event/candle watermarks.
- Duplicate or concurrent events cannot duplicate terminal closure or reopen a closed trade.
- A gap through a stop records the stop trigger separately from observed/reference price and actual fill.
- T1/stop source chronology is respected without lookahead; unresolved candle ordering remains explicitly unknown.
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
