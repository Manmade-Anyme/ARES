---
task_id: "TASK-20261030"
date: "2026-10-30"
status: "approved for implementation and bounded 26–30 Oct recovery; production migration and capture activation require separate approval"
author: "Codex PM Agent"
objective: "Move the performance digest earlier and report one-lot option reference P&L only from observed entry and final-exit premiums."
---

# Directive: Weekly Performance Report P&L

## 1. Context and problem

The weekly performance digest is checked only during the final minute of the
session (15:29–15:30 IST). A missed poll in that minute can skip delivery. The
user requested that the digest move one minute earlier while retaining a retry
opportunity through close.

The current report also shows rupee P&L estimates derived from spot movement and
planned option brackets. These are not observed option-price outcomes. The
user selected a single one-lot reference-premium metric based on the observed
option premium at entry and the observed premium at final exit. Spot T1/T2 remain
milestone context and do not allocate partial P&L.

A manual report for 26–30 Oct 2026 was delivered on 30 Oct at approximately
16:19 IST. The production audit found no stored option-price captures for that
week's 16 trades or the 381 closed rows checked. A separate read-only Dhan
history review returned matching pinned-security completed-minute bars
(approximate marks) for 11 trades.
Five trades pin the 27 Oct expiry, which was already expired. Dhan's rolling
history endpoint rejects expiry code 0 (current/near expiry); code 1 is next
expiry, so a same-strike minute from that series cannot prove it belongs to the
stored 27 Oct contract. The user approved a one-time recovery for this exact
weekly cohort, but the all-or-nothing write must remain blocked until all 16
pairs have verified contract identity. The 11 validated pairs are approximate
market references, not contemporaneous fills.

## 2. Objective and scope

1. Make weekly and month-end performance reports eligible from 15:28 IST until
   the existing 15:30 session close. Send once on the first successful poll;
   retain retries while delivery fails and the window remains open.
2. Preserve trade count, win rate, and spot-point metrics, including net points,
   average points, best, and worst. The overall summary must show **Net Spot
   Points** alongside **Net One-Lot Option Premium Points** and their **₹
   equivalent**.
3. Remove the spot-delta rupee estimate and the planned-bracket one-lot rupee
   estimate from report output. Do not present either as a fallback.
4. Add one-lot option reference P&L from the same contract's observed entry
   premium to observed final-exit premium. Show both the net premium-point
   change and its rupee equivalent, using the contract's recorded lot size.
   Count one lot per trade; do not multiply by suggested/final trade lots. T1/T2
   spot milestones provide context only; do not book partial exits or weight
   P&L by milestone fractions.
5. Keep setup breakdowns for trade count, win rate, and spot points. Do not
   retain their spot-derived currency estimates. The new option P&L is an
   overall period metric; setup-level premium P&L is outside this scope.
6. Backfill only the 16 report-eligible trades closed from 26 Oct 00:00 IST
   through 31 Oct 00:00 IST, using validated Dhan completed-minute closes for
   each pinned contract. Proceed only if all 16 pairs are verifiable; otherwise
   write none and leave the period unavailable. No other historical rows are
   in scope.

## 3. Acceptance criteria

- The shared report trigger begins at 15:28 IST and remains eligible until
  15:30 IST, so a delayed poll can still deliver in the next minute.
- Both existing rupee estimates are absent from weekly and monthly reports,
  including setup rows.
- The overall summary shows net spot points, net one-lot option premium points,
  and the rupee equivalent together.
- For each eligible trade `i`, calculate one-lot premium points as
  `final_exit_premium_i - entry_premium_i`; sum those values for the period.
  Calculate the rupee equivalent as
  `Σ((final_exit_premium_i - entry_premium_i) × recorded_lot_size_i)`.
  Use one lot per trade regardless of the recorded suggested/final trade-lot
  count.
- Entry and final-exit observations have timestamps, identify the same
  contract, and are chronologically valid. T1/T2 events do not change the
  calculation or create partial booking.
- Every report shows option-price coverage as priced eligible trades / total
  eligible closed trades, with percentage.
- Both option premium-point total and its rupee equivalent are shown as
  **Unavailable** unless every eligible trade has valid entry and final-exit
  price evidence. Missing prices are never treated as zero, inferred from
  spot/delta, or substituted with planned SL/target premiums. Coverage remains
  visible when either value is unavailable.
- Label both option figures as reference-premium metrics, not broker-fill P&L
  or realized account return. Do not imply that fees, slippage, or actual
  execution fills are included.

## 4. Data requirements

For each eligible closed trade, persist immutable reference evidence linked by
stable trade ID:

- option contract/security identity;
- observed entry premium and timestamp;
- observed final-exit premium and timestamp;
- contract lot size applicable to the trade;
- observation source and validity/quality status.

Both observations must refer to the same contract. Rejected, stale, non-finite,
non-positive, missing, or out-of-order observations do not count toward price
coverage. The eligible-trade denominator must match the report's closed-trade
filters. The one approved historical exception is the exact 26–30 Oct 2026
cohort above, using complete Dhan minute bars with verified contract identity
and preserving provider and bar provenance as approximate evidence. Current
read-only evidence covers only 11/16 trades, so the atomic write is not
eligible yet. All other rows without timestamped prices remain unpriced; never
infer marks from spot movement, delta, or planned bracket metadata.

## 5. Boundaries

- Capture is reference-only and must be isolated from option ATR enrollment,
  stop/protection state, target handling, position sizing, and broker order
  submission.
- Do not enable or bypass the existing option-ATR approval gate to obtain
  reference prices. Any required storage or capture path needs separate review
  and explicit rollout authorization.
- Do not add partial-exit weighting at T1/T2, broker-fill claims, inferred
  historical prices, or a spot-derived currency fallback.
- The only approved historical data write is the all-or-nothing, service-role
  backfill of the complete 16-row 26–30 Oct cohort, after PR #151 is merged and
  its migration is applied and verified. The reviewed dry-run must validate
  16/16 exact contract pairs; any shortfall aborts without writes. There is no
  general historical-enrollment path.

## 6. Rollout gate

Implementation and the exact-week recovery were approved. Before numeric
option premium points or a rupee equivalent are exposed in a production digest:

1. Implement reference capture behind an explicit gate independent of
   `option_atr_enabled`; confirm by review that it cannot alter trade protection
   or execution behavior.
2. Validate capture-only records on closed paper/reference trades, including
   same-contract identity, timestamps, lot size, terminal-exit semantics, and
   coverage accounting.
3. Require complete price coverage for a period before displaying either
   aggregate option metric. Until then, display both as `Unavailable` plus
   `N/M` coverage and percentage.
4. The human merges PR #151 and verifies its exact merged SHA. Obtain separate
   human approval before applying the migration. Then review the dry-run; only
   execute the already-authorized 16-row backfill if it validates 16/16 exact
   contract pairs. Any shortfall leaves all rows unchanged and the report
   unavailable. Obtain separate human approval before enabling future capture.
   Do not apply the migration, activate capture, or post a Discord report as
   part of this feature implementation.

The manually delivered 30 Oct report is complete. The approved backfill may
populate only its exact 26–30 Oct cohort after the rollout gates above; future
capture remains independently gated.
