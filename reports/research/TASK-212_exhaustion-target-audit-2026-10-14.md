# Exhaustion Target Audit — October 14, 2026

**Decision: retain T1 at 18 points with SL at 30. Neither T1=24 nor T1=30 met the predeclared improvement rule. PR #130 requires no further application change.**

## Comparison

Replayed the same 165 recorded exhaustion entries from July 20–October 12 against 59 frozen Dhan sessions. Entry references and causal first-full-minute alignment remain fixed. SL is 30 for every candidate; T1 is 18, 24 or 30. At T1 the stop moves to entry. Scoring is initial SL −1, T1 +1, T2 +2 total, and T1 then breakeven +1. Pending before T1 is unscored; pending after T1 earns +1 but remains unfinished.

The primary T2 approximation retains the recorded T2 when it remains beyond the new T1; otherwise it substitutes the existing 40-point fallback. This is conditional on missing historical structure, as explained below.

| T1 | T1/SL | Earned score | Initial SLs | T1 then breakeven | T2 | Unfinished after T1 |
|---|---:|---:|---:|---:|---:|---:|
| **18** | **0.60** | **+54** | **72** | **59** | **33** | **1** |
| 24 | 0.80 | +39 | 80 | 50 | 34 | 1 |
| 30 | 1.00 | +29 | 88 | 36 | 40 | 1 |

Compared with T1=18, T1=24 converts eight T1/breakeven trades into full SLs and improves one to T2: −16 + 1 = **−15 score points**. T1=30 converts sixteen into full SLs and improves seven to T2: −32 + 7 = **−25**. Every original T2 finish remains T2 in these comparisons. Higher targets delay breakeven protection; extra T2 finishes do not offset the additional stop-outs under the user's scoring.

## Structural T2 uncertainty and reliable subsets

ARES chooses the nearest structural level strictly beyond T1, otherwise a 40-point fallback. The signal export stores the selected T2, not the whole level list. Code inspection and read-only Supabase inspection confirm that collection features preserve nearest support/resistance distances, not the complete candidate levels. Therefore a larger T1 can expose an unknown next level.

At T1=24, five recorded T2s are at or below T1; at T1=30, eight are. Using 40 for these is an assumption, not a reconstructed next structural level. Lowering the older 36 trades from recorded T1=24 to18 could also expose an unknown nearer T2. To address both limitations, compare identical subsets:

| Cohort / T2 treatment | Entries | T1=18 score | T1=24 score | T1=30 score |
|---|---:|---:|---:|---:|
| Full historical cohort, recorded-T2 approximation | 165 | +54 | +39 | +29 |
| Recorded T1=18 only, same approximation | 129 | +51 | +36 | +32 |
| Recorded T1=18 and recorded T2>30; T2 reliably unchanged | 121 | **+51** | **+36** | **+32** |
| Controlled sensitivity: fixed T2=40 for every entry | 165 | +66 | +52 | +44 |

The reliable common cohort also favors T1=18. The fixed-40 sensitivity confirms the ranking under a consistent second-target distance. It changes the T2 policy, often shortening distant targets; it does not justify adopting fixed T2=40 in this PR.

## Price-distance check, separate from the score

| T1 | Sum of configured distance across SL outcomes | Sum of terminal barrier movement |
|---|---:|---:|
| 18 | 2,160 | −375.45 |
| 24 | 2,400 | −536.60 |
| 30 | 2,640 | −368.90 |

The second diagnostic assigns −30 to SL, 0 to T1/breakeven and the T2 distance to T2 finishes; it excludes the unfinished trade. It is a hypothetical index-barrier reference, not a fill model or cash P&L: it ignores gaps beyond barriers, charges and any booking at T1. T1=30 is 6.55 points less negative than 18 on this diagnostic despite its worse score. That small advantage does not persist with fixed T2=40 (−360 / −520 / −440 for T1=18/24/30). The evidence supports retaining 18 under the user's defined score; it does not establish the best monetary return.

## Observation-window checks

Late-session flat candles and closing jumps remain a feed limitation. Same-day and pre-15:15 checks censor observations; they do not model forced liquidation. Unfinished includes pending before T1, pending after T1 and entries without a usable window.

| Cohort / window | T1=18 score / unfinished | T1=24 score / unfinished | T1=30 score / unfinished |
|---|---:|---:|---:|
| All 165, same day | +41 / 29 | +25 / 36 | +16 / 40 |
| All 165, before 15:15 | +43 / 48 | +27 / 54 | +20 / 61 |
| Current-target 129, before 15:15 | +35 / 34 | +20 / 40 | +15 / 45 |
| Reliable common 121, before 15:15 | +37 / 31 | +22 / 37 | +17 / 42 |

Higher T1 both lowers earned score and leaves more unfinished observations. Full-history monthly scores also show no month improved by a higher T1: July +5/+5/+1, August +25/+17/+11, September +27/+20/+20, October through 12 −3/−3/−3 (ordered T1=18/24/30).

The separate October 13 check contains only three entries and is excluded from selection. All three target settings score0: one T2 and two SLs, resolving before 15:15. It cannot distinguish the candidates.

## Decision rule and limitations

Before examining outcomes, the protocol required a higher T1 to improve earned score on both the 129 current-target and 121 reliable common cohorts under full-history and pre-15:15 windows, with no increased unfinished count in the censored common cohort. Neither candidate meets that rule. Keep **SL 30 / T1 18 / existing structural T2, fallback 40 / exhaustion minimum R:R 0.60**.

This is a fixed-entry event study. It does not reproduce changed cooldowns, concurrent positions, later signal availability or broker fills. It omits the partial entry minute; signal 475's recorded stop occurred in that interval. Intrabar order uses conservative open-first/stop-first rules. Previously inspected history is not an untouched holdout. A +1 target score does not distinguish 18 from30 price points, and a −1 SL score does not quantify financial loss. The conclusion concerns the specified scoring and observation windows, not maximum monetary profitability.

## Evidence and reproducibility

Local evidence archive: `Exhaustion Target Audit 2026-10-14` under the user's Daily Market Observation folder. It contains the frozen protocol, research-only calculation script, per-trade ledgers, aggregate tables, independent checks and October 13 appendix. Application/runtime code is unchanged by this audit.

Frozen inputs from `exhaustion-sl-loop-2026-10-13`:

- `cohort.csv`: SHA256 `5688169756f6fda7433397e186f4164c4bb2c110fc904f38f0efb272dacd2213`
- `dhan_raw.json`: SHA256 `b571e27f149eece75039dea108a744a37702f51c7c944990040bdcd267bbcdfc`
- Verified `replay.py`: SHA256 `9e7375d5fb914036d5c32e5785ff554ade6526f3049753a3ae17b71eeaa7fe15`

Controls reproduce −47 with original stops/targets and +54 with SL30/recorded targets. The standardized T1=18 comparison has the same full-history outcomes and scores as that +54 control. Sensitivity scores differ because older entries had recorded T1=24. Raw datasets and individual trade identities are kept in the local evidence archive.
