# ADR-TASK-20261030: Independent One-Lot Option-Premium Reference Accounting

- **Status**: Approved for implementation — production migration application and runtime activation require separate approval
- **Date**: 2026-10-30
- **Task ID**: TASK-20261030
- **Author**: Software Architect Agent
- **Target Branch**: `feature/TASK-20261030-weekly-report-window`
- **Implementation**: Authorized 2026-10-30
- **Production migration application / activation**: Requires separate human approval

---

## 1. Context and Problem Statement

ARES keeps spot movement P&L in `trade_analytics.pnl_points`. The current performance report derives option rupees from spot points and delta, and its one-lot bracket estimate selects the option SL or T1 price from the trade outcome (`reports.py:131-187`). Those values are projections, not observed option premiums at the reference entry and final exit.

There is already an option reference-accounting implementation, but it is coupled to the candidate option-premium ATR policy. `position_manager.py:343-371` enrolls a trade only when `option_atr_enabled`, the approved ATR version, service credentials, and the database capability all pass. The same `option_trailing` context drives premium monitoring and protection (`main.py:127-135,400-455`). Its `one_lot_reference_v1` formula weights 50% at T1, another 30% at T2, and the remaining 20% at final exit (`position_manager.py:1251-1273`).

The installed accounting schema is also tied to those semantics. `trade_analytics.option_pnl_points` is documented as weighted one-lot-equivalent premium points, and the capability, lifecycle, correction, and recovery functions select `option_trailing.policy_version = option_atr_v1` (`migrations/20261029_option_trade_accounting.sql`). A reported `option_reference_accounting_v1` readiness result therefore does not establish readiness for a separate hold-through-exit contract.

The selected reporting behavior is a hypothetical one-lot reference position held in full from an observed option entry premium through the existing trade's final exit. T1 and T2 remain spot milestones. Existing spot P&L and exit/protection behavior remain unchanged.

## 2. Goals and Non-Goals

### Goals

- Preserve existing spot `pnl_points` and its calculation.
- Record exact-contract option price evidence at reference entry and final exit.
- Report net option premium points and the one-lot rupee equivalent.
- Keep the hypothetical quantity at one full lot through T1 and T2.
- Record T1/T2 as spot-hit context only.
- Recover missing price observations without blocking the market-protection loop.
- Keep the accounting path independent of option ATR activation and protection.

### Non-Goals

- Enable `option_atr_enabled` or change `option_atr_approved_version`.
- Change index or option stops, ATR calculations, exit triggers, dispatch, or broker execution.
- Claim a broker fill or realized net-of-costs P&L. The reference uses observed LTP marks and excludes fees, spread crossing, and market impact.
- Reinterpret the existing weighted `one_lot_reference_v1` records or retroactively enroll historical trades.
- Change ML outcome labels or trade scores.

## 3. Decision

Introduce an accounting-only context and configuration gate, separate from `option_trailing`:

- `market_context.option_reference` is the canonical state for the new formula.
- Formula version: `one_lot_hold_to_exit_v1`.
- New configuration: `option_reference_accounting_enabled`, independent of all option ATR settings. It defaults to disabled until the additive schema is installed and the user separately authorizes activation.
- Enrollment requires a valid pinned option contract, a valid lot-size snapshot, backend service credentials, and the new capability check. Capital and suggested lot count do not determine this one-lot display reference.
- The accounting capture path may read option prices, but it must never create `option_trailing`, call the option ATR transition writer, evaluate or ratchet an option stop, or change any protection flag.

The full reference lot remains open until the existing spot lifecycle emits its final terminal exit. T1 and T2 do not sell or book any portion of the one-lot reference.

### Alternatives Considered

1. **Reuse the current ATR-linked `option_trailing` lifecycle:** rejected because its enrollment gate also controls premium monitoring/protection, and its existing formula books 50/30/20 at T1/T2/exit. Reuse would couple report accounting to protection and preserve the wrong semantics.
2. **Create a separate table:** deferred because the record is one-to-one with an existing trade and the existing locked `trade_analytics` row can hold both capture evidence and a queryable formula-specific points column. A table becomes justified only if capture history needs independent event retention or multi-contract references.
3. **Continue estimating option P&L from spot points and delta:** rejected for this report because it does not use observed option entry and exit prices.

## 4. Reference-State Contract

Illustrative `trade_analytics.market_context.option_reference` object:

```json
{
  "formula_version": "one_lot_hold_to_exit_v1",
  "revision": 1,
  "status": "TRACKING",
  "reference_lots": 1,
  "contract": {
    "security_id": "51333",
    "exchange_segment": "NSE_FNO",
    "expiry_date": "2026-11-03",
    "strike": 22550,
    "option_type": "PE",
    "lot_size": 65
  },
  "entry_event_at": "2026-10-30T09:20:00+05:30",
  "entry": {
    "price": 140.25,
    "observed_at": "2026-10-30T09:20:01+05:30",
    "source": "dhan_last_trade",
    "approximate": false
  },
  "milestones": {
    "T1": {"event_at": "2026-10-30T09:31:00+05:30", "spot_target": 22870, "spot_price": 22871.2, "source": "completed_candle_touch"},
    "T2": null
  },
  "exit_event_at": null,
  "exit_reason": null,
  "exit": null,
  "premium_points": null,
  "capture_quality": null,
  "applied_events": {}
}
```

Contract fields are immutable after enrollment. The entry event time for a `SIGNAL_REFERENCE` trade is its canonical signal/analytics `entry_timestamp`; an execution fill must not be inferred from a suggested lot count. If a different reference-entry definition is required for execution-backed rows, it must be specified as a separate cohort before enrollment.

Entry and exit price objects retain the event time, observation time, source, and approximation flag. `premium_points` remains null until valid entry and exit prices are both present. The state object records evidence and reporting fields only; it does not represent an order, inventory, or a protective stop.

## 5. Price Capture Contract

### 5.1 Pinned contract

Use the contract selected at the signal (`options_sizing.security_id`, exchange segment, expiry, strike, and option type). Snapshot `options_sizing.lot_size` at enrollment. Do not switch to a different strike or expiry when a quote or history request fails. Reject incomplete, invalid, or expired pins.

`options_math.py:203-257` distinguishes `reference_premium` (the selected chain LTP) from `entry_limit` (a derived buy-limit value). Neither the derived limit nor an un-timestamped chain LTP qualifies as an observed entry price for this formula.

### 5.2 Entry and exit observations

- For entry, capture the pinned contract's Dhan last-traded price at the signal-reference event.
- For final exit, capture the same contract at the final exit event already selected by ARES's current spot lifecycle.
- Use the event timestamp from the existing lifecycle event, not the later database-write time.
- Prefer a pinned LTP observation whose last-trade timestamp is within five seconds of the event. `fetchers/price_fetcher.py:412-447` already rejects stale last trades; `execution/reference_prices.py:105-130` provides a nearby-quote selector with the same five-second bound.
- If no eligible quote exists, use only the matching completed one-minute option close, sampled after that minute ended, and set `approximate: true`. The existing minute-normalization and close validation in `execution/reference_prices.py:30-85` is the reference behavior.
- After quote or minute-history requests complete, validate returned observations against a fresh post-fetch cutoff; preserve each provider observation timestamp and quality/completion metadata unchanged as provenance.
- Persist the terminal-exit marker promptly, but defer exit-price lookup and capture until valid entry evidence exists. If the exit observation precedes the entry observation, leave exit evidence unset and retry against the original terminal event timestamp.
- If neither source yields valid evidence, keep that side null and mark coverage incomplete. Do not substitute delta, `entry_limit`, option SL/T1/T2 projections, or zero.

The API returns LTP, not a guaranteed executable bid or ask. The report must name the metric as a captured/reference premium change, not a fill or realized option P&L. Bid/ask execution-cost modelling is outside this ADR.

### 5.3 T1 and T2 milestones

Persist the existing spot lifecycle's T1/T2 touch evidence: target spot, observed spot price (when available), source timestamp, and time basis. Keep the legacy `target_milestones` object unchanged and mirror report-specific milestone events under `market_context.option_reference_milestones` so a restart can replay an uncommitted capture. Milestones are context only. They do not require option price samples and do not create financial legs or alter the one-lot quantity. The reported milestone timestamps retain the existing candle/tick precision; they must not be described as broker fills.

## 6. P&L and Performance-Report Contract

For a long call or put held as a one-lot reference through final exit:

```text
net_option_premium_points = exit_option_premium - entry_option_premium
one_lot_option_rupees = net_option_premium_points * captured_lot_size
```

Do not apply spot-direction signs or 50/30/20 milestone weights to the option premium formula. Keep `trade_analytics.pnl_points` unchanged as the existing spot result. Keep the old `option_pnl_points` weighted-v1 metric unchanged.

The weekly performance report must present, for the new formula:

- Existing spot P&L points, unchanged.
- Net option premium points.
- One-lot rupee equivalent using the captured historical lot size.
- T1/T2 hit status and spot context, without implying partial booking.
- Coverage counts: exact LTP pairs, pairs with an approximate minute close, and rows with unavailable entry/exit evidence.

Rows without both prices are null/unavailable, not zero, and are excluded from premium totals. A measured break-even is zero only when both prices exist and are equal. Do not combine old weighted-v1 values, delta estimates, or approximate-history rows into an unlabeled single total. Exact-quote and approximate-close subtotals must be distinguishable in the report.

## 7. Persistence and RPC Contract

No new table is required. Use the existing `trade_analytics.market_context` JSONB for the evidence and add a distinct nullable scalar `trade_analytics.option_reference_pnl_points numeric` for the new formula. Its value is per-unit option premium points (`exit - entry`); the rupee equivalent is derived using the captured lot size. Keeping this scalar separate avoids mixing it with the existing `option_pnl_points` weighted 50/30/20 values.

Add an additive migration with these backend-only contracts:

### `apply_option_reference_capture`

```text
apply_option_reference_capture(
  p_trade_id uuid,
  p_expected_revision integer,
  p_event_id text,
  p_event jsonb
) -> jsonb
```

- Enrollment is written atomically with a new trade entry. Accept only versioned capture events: entry capture, T1/T2 spot milestone, final-exit marker, or final-exit price capture during recovery.
- Require a positive expected revision, stable non-empty event ID, valid event object, and immutable contract pin.
- Apply one transaction with row locks in the existing lifecycle writer order; same event ID and same payload is an idempotent success, while reuse with different payload or conflicting non-null evidence is rejected.
- Merge only `market_context.option_reference` and set `option_reference_pnl_points` only when entry and exit prices validate. Missing evidence remains null.
- Never update `active_trades.state`, `stop_loss`, `exit_price`, or `exit_timestamp`; never update `trade_analytics.result_state`, `score`, `exit_price`, `exit_timestamp`, `pnl_points`, legacy `option_pnl_points`, ML labels, or alert outboxes.

### `list_incomplete_option_reference`

```text
list_incomplete_option_reference(p_limit integer, p_offset integer) -> jsonb
```

- Return bounded pages of explicitly enrolled `one_lot_hold_to_exit_v1` rows with missing entry/exit evidence or a durable T1/T2 milestone waiting for its canonical capture event.
- Include the pinned contract, original event time, terminal/open state, current reference revision, and only the evidence needed for recovery.
- Exclude all rows that lack the new formula marker; do not auto-enroll historical rows.

### `option_reference_capture_capabilities`

Return a distinct schema version (`one_lot_hold_to_exit_v1`) and `ready` only when the new scalar column, both RPCs, required service-role privileges, and table access are present. The existing `option_reference_accounting_v1` check is insufficient: its SQL predicates and recovery scan are coupled to `option_trailing` and `one_lot_reference_v1`.

Revoke execution from `PUBLIC`, `anon`, and `authenticated`; grant only to `service_role`, following the existing lifecycle migration. Do not alter the current ATR lifecycle or correction functions to accept the new formula.

## 8. Runtime Isolation and Recovery

- Enrollment must not set `market_context.option_trailing`, even when the pinned contract is valid.
- The option capture worker is separate from `update_option_atr_trailing` and from ATR history calculation. It may call the pinned quote/minute-price APIs but must not feed observations into ATR state.
- Record entry, T1/T2, and exit event identities promptly. Mirror T1/T2 report events outside the legacy spot milestone shape, then replay durable evidence after restart. Run network price lookup and repair in a bounded background task, not in the protection path.
- Persist a terminal-exit marker promptly, but do not persist an exit-price leg before valid entry evidence exists. Keep an exit whose observation time precedes the entry observation retryable; recover it against its original terminal event time.
- On a closed trade with a missing exit capture, recovery may fetch only the pinned contract's matching historical minute. It cannot revise the spot exit or trade outcome.
- A missing option price, database failure, or capability failure must leave the existing spot monitoring, stop checks, exit, and shutdown behavior available.

## 9. Historical Data Limits

Existing rows may contain `options_sizing.premium`, but it lacks a saved quote timestamp/source. Existing `option_trailing.original_option_entry` is populated from `entry_limit`, a derived limit, not a captured entry observation. Existing v1 milestone records therefore cannot be relabeled as this held-through-exit formula.

A historical audit may attempt a separate reconstruction only when the original security ID, expiry, strike, type, event times, and matching option-minute bars are all available. A minute close is reconstructed approximate evidence, not the exact contemporaneous LTP. Provider retention and data availability are not guaranteed. Missing pins or bars leave the row unavailable. No automatic migration, delta fallback, or formula conversion is approved here.

## 10. Verification Seams and Definition of Done

Implementation should add focused tests at these public seams; this ADR does not run or authorize tests:

1. **Gate isolation:** accounting-only enrollment with option ATR disabled creates `option_reference` but no `option_trailing`; no option ATR fetch/update/stop path is invoked. ATR-enabled behavior remains unchanged.
2. **Capture selection:** exact pinned security ID only; a quote within five seconds is accepted, a stale/wrong-contract quote is rejected, and a matching completed one-minute close is accepted only as approximate.
3. **Formula and milestones:** full one-lot exit-minus-entry result for CE and PE, no direction inversion, no T1/T2 weighting, correct rupee multiplication, and unchanged spot P&L.
4. **Missing data:** missing entry/exit remains null and counts as unavailable; equal observed premiums yield a true zero; incomplete rows do not enter option totals.
5. **Persistence:** RPC retries are idempotent; evidence cannot be overwritten by a conflicting event; accounting writes cannot change lifecycle fields, ML labels, or legacy weighted values.
6. **Recovery/report:** bounded recovery targets only new formula rows; report separates exact/approximate/unavailable coverage and does not mix legacy formulas.

The work is ready for a release proposal only after the additive migration, capability probe, focused regression coverage, and report output are independently verified. Supabase migration application and runtime activation require their own explicit approval.

## 11. Approval and Rollout Gate

The user approved implementation on 2026-10-30. Deliver the implementation as one feature change with the additive migration, tests, report changes, and documentation. The new accounting flag and database capability must be independently gated. Production migration application and runtime activation require separate human approval. `option_atr_enabled` and the approved ATR version remain unchanged throughout this rollout.

The implementation remains on the feature branch. Local verification for the review follow-up passed: `python -m pytest tests/ -q` (2,110 passed, 45 subtests passed, 12 warnings); focused capture coverage is 100% statements and branches (29 tests, 230 statements, 70 branches); the standalone report self-check, isolated PGlite migration/RPC check, Python compilation, Node syntax check, and `git diff --check` passed. No production migration was applied and the capture flag remains off; those rollout steps still require separate approval.
