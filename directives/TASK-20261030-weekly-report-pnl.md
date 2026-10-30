---
task_id: "TASK-20261030"
date: "2026-10-30"
status: "approved for implementation; production migration and activation require separate approval"
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
16:19 IST. The production audit found no option-price capture for the 16 trades
in that week or the 381 closed rows checked, so the historical option P&L cannot
be reconstructed from recorded evidence.

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
filters. Existing rows without these observations remain unpriced; do not
backfill them from spot movement or planned bracket metadata.

## 5. Boundaries

- Capture is reference-only and must be isolated from option ATR enrollment,
  stop/protection state, target handling, position sizing, and broker order
  submission.
- Do not enable or bypass the existing option-ATR approval gate to obtain
  reference prices. Any required storage or capture path needs separate review
  and explicit rollout authorization.
- Do not add partial-exit weighting at T1/T2, broker-fill claims, historical
  estimates, or a spot-derived currency fallback.
- No Supabase production schema/data changes are included in this directive.

## 6. Rollout gate

The design is **approved for implementation**. Before numeric option premium
points or a rupee equivalent are exposed in a production digest:

1. Implement reference capture behind an explicit gate independent of
   `option_atr_enabled`; confirm by review that it cannot alter trade protection
   or execution behavior.
2. Validate capture-only records on closed paper/reference trades, including
   same-contract identity, timestamps, lot size, terminal-exit semantics, and
   coverage accounting.
3. Require complete price coverage for a period before displaying either
   aggregate option metric. Until then, display both as `Unavailable` plus
   `N/M` coverage and percentage.
4. Obtain separate human approval before applying the migration or enabling the
   capture path. Do not promote, merge, or deploy as part of this directive.

The manually delivered 30 Oct report is complete. This directive applies to
future reporting behavior only.
