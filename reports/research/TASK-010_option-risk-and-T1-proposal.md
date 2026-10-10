# TASK-010 — Proposal for option-based risk and T1 calculations

Date: 2026-10-06
Status: Exploring; no production implementation or validated parameter choice.
Scope: User clarified that option premium drawdown must control risk, even while
spot remains inside the original trade setup. Focus exclusively on initial SL/T1.
This proposal changes the options layer from a displayed reference into an
independent risk/exit policy. Existing spot backtest results do not validate it.

## Required calculation sequence

1. Identify the exact contract/expiry and obtain synchronized, fresh spot and
   option bid/ask/depth. Estimate entry with the ask and expected fill depth;
   re-evaluate against the actual fill before committing the final risk plan.
2. Determine a candidate premium SL from the selected option's own historical
   fluctuations or defended premium structure. Examples to compare in research:
   a confirmed option swing low with a spread/noise allowance, or a multiple of
   recent option true range. All inputs must precede signal publication.
3. Size quantity to the rupee risk budget at that premium SL, with costs and
   adverse execution allowances. Reject the trade if the minimum lot cannot fit;
   never tighten the chosen stop merely to make one lot affordable.
4. Set a required premium T1 from net reward/risk, and independently evaluate
   whether the spot setup offers sufficient option reward under calibrated
   expiry, elapsed-time, IV and execution scenarios. Skip weak candidates.
5. Compare candidate strikes using spread/depth, option fluctuation relative to
   risk budget, expiry exposure and conditional reward at spot T1. A delta band
   alone does not establish that a contract is suitable for this account size.
6. Execute exits on the actual option risk/target conditions. A validated spot
   invalidation condition can be an additional early exit, but it must not
   postpone an option risk exit. This is a proposed strategy policy, not a change
   made to PositionManager in this investigation.

## Formulas for a bought option

Let:

- C = available trading capital; r = configured fraction at risk.
- E = actual option entry price per unit; S = proposed premium SL reference.
- Q = verified contract lot quantity; n = integer number of lots.
- L(n) = adverse stop execution allowance per unit for quantity n*Q.
- F(n) = estimated entry/exit fees and taxes for that quantity and prices.
- R = chosen minimum net reward/risk; it requires validation, not an arbitrary
  value inserted into production.

Then:

```text
B = C * r
PlannedLoss(n) = n * Q * (E - S + L(n)) + F(n)
n = largest affordable nonnegative integer with PlannedLoss(n) <= B
```

Fees, depth and impact can depend on quantity, so treating them as constant per
lot is only a simplifying approximation. For candidate lots, solve/check the
inequality explicitly. Require E>S>0 and valid contract prices/ticks.

For T1, use a conservative executable exit-price estimate X. Require:

```text
NetProfitAtT1(n) = n * Q * (X - E) - TargetExitCosts(n)
NetProfitAtT1(n) >= R * PlannedLoss(n)
RequiredT1ExitPrice = E + (R * PlannedLoss(n) + TargetExitCosts(n)) / (n * Q)
```

The target exit-cost term includes entry and target-exit fees/taxes and any price
allowance not already embedded in X. Avoid double-counting bid/ask spread or
slippage: either use an executable price estimate or subtract the corresponding
allowance. Do not calculate T1 when n=0; the decision is to skip that candidate.

## Pricing feasibility at the spot T1

Use a market-calibrated European-option model with exact expiry timestamp,
appropriate underlying/forward and interest/dividend conventions. First confirm
it reproduces the current option reference within quote tolerance. Provider IV
and Greek time conventions must be verified; blindly inserting provider IV into
a different expiry-time convention can produce a wrong price at entry.

For each predeclared elapsed-time and IV scenario j, calculate option value
V(S_spot_T1, remaining_time_j, IV_j). Convert theoretical value into an estimated
exit bid for the expected quantity. Compare conditional net reward with planned
premium-stop risk. Scenario ranges must come from prior data and documented
stress assumptions, not from future observations of the current trade.

This evaluates whether entering is worthwhile; it cannot guarantee a future
premium. Near expiry, calibrated repricing is preferable to an unbounded local
delta/gamma extrapolation. A local Greek expansion can still be a comparison
baseline, not the sole source of trade risk levels.

## Concrete feasibility check using saved real data

The option minute bars below are from the exact contracts already fetched for
the latest audit. For each, compute the arithmetic mean of true range over the
last 14 completed one-minute bars strictly before the signal minute:

`TR = max(high-low, abs(high-previous_close), abs(low-previous_close))`.

This is a simple rolling true-range mean, not Wilder's smoothed ATR. An
illustrative multiplier 1.5 is used solely to expose sizing consequences; it is
not a validated or recommended stop parameter.

| Item | Latest 22,700 CE | Latest closed 22,600 PE |
| --- | ---: | ---: |
| Recorded entry LTP reference | 39.95 | 50.30 |
| Last-14-bar mean true range | 2.89643 | 5.12857 |
| Existing delta-based stop distance | 4.83730 | 5.41008 |
| Recorded 4% cash risk budget | 318.76680 | 367.06040 |
| Illustrative 1.5 x true-range stop distance | 4.34464 | 7.69286 |
| Corresponding illustrative premium SL | 35.60536 | 42.60714 |
| Planned one-lot loss, 65 units, BEFORE costs | 282.40179 | 500.03571 |
| Risk-permitted lots BEFORE costs | 1 | 0 |

The closed PE's current stop distance is approximately 1.055 times its recent
minute true range. Under the illustrative wider stop, one lot exceeds the budget;
the rule must reject it or find a suitable alternative contract, not keep one
lot by forcing the stop tighter. No claim is made that 1.5 times true range would
improve this trade's outcome or overall returns.

For the latest CE, the original delta-projected one-lot loss is 314.42450 against
budget 318.76680, leaving only **4.34230 rupees** for all charges and adverse
execution. The live sizing formula excludes those costs entirely.

## Evaluation before automation

Compare at least these candidate policies on the same eligible spot signals:

- Existing delta-based premium SL/T1, used as actual option exit triggers.
- Option fluctuation/structure-based SL, cash-risk sizing and net R-based T1.
- The same option risk policy with calibrated time/IV feasibility screening and
  alternative-strike evaluation.

Keep a holdout by trading day and expiry session; tune only on earlier data.
Measure option drawdown, premature-stop/recovery frequency, T1-first rate, net
P&L after costs, skipped-trade rate and realized loss versus budget. Separate
expiry-day and non-expiry conditions. Use observed selected-contract prices and
price/depth-aware execution assumptions. Minute bars cannot resolve event order
when premium SL and T1 occur within the same minute; flag ambiguous bars and
evaluate conservative ordering or use timestamped quotes/ticks.

An operational stop is a planned exit threshold, not a guaranteed loss cap.
Gaps, spread changes and missing liquidity can cause worse execution. For a long
option, the premium paid is the contractual capital at risk if no effective exit
occurs. If an absolute loss bound is required, it constrains exposure/structure
separately from stop-distance sizing.

## Sources

- [OIC model inputs and limits](https://www.optionseducation.org/advancedconcepts/black-scholes-formula)
- [Dhan quote, depth and Greek fields](https://dhanhq.co/docs/v2/option-chain/)
- Existing exact-contract audit: `TASK-010_latest-option-audit-2026-10-06.md`.

No final stop multiplier, reward ratio or scenario horizon has been selected.
Production trading code and exit behaviour are unchanged.
