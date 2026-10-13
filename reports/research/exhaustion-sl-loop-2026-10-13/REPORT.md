# Exhaustion Stop-Loss Audit: Three Data-Derived Wider Stops

## Executive Summary & Core Verdict

This research performs a frozen-selection empirical audit of wider stop-loss (SL) distances for signals produced by the `EXHAUSTION_REVERSAL` detector across 165 matched production trades recorded between July 20, 2026, and October 12, 2026. The baseline production configuration operates with tight initial stop losses (median 10.0 points, maximum 12.0 points; target 1 at 18.0 or 24.0 points).

Three candidate wider stops were derived strictly from the **Development partition** (July 20 to August 31, 2026) using Maximum Adverse Excursion (MAE) quantiles of qualifying training recoveries ($n=8$) that reached Target 1 (T1) after exceeding their original stop loss, bounded within $[13, 30]$ index points:
1. **Round 1 — SL = 22 points** ($q_{50} = 20.125 \to \lceil 20.125 \rceil + 1 = 22$)
2. **Round 2 — SL = 29 points** ($q_{90} = 32.845 \to \lceil 32.845 \rceil + 1 = 34 \to$ bounded to 30; duplicate resolved to 29; sorted ascending)
3. **Round 3 — SL = 30 points** ($q_{75} = 29.3125 \to \lceil 29.3125 \rceil + 1 = 31 \to$ bounded to upper cap 30)

*Note: Stop widths 29 and 30 yield nearly identical behavior due to the pre-declared cap at 30 and the small development qualifying sample ($n=8$).*

```
+-------------------------------------------------------------------------------------------------------------------+
| VARIANT       | TRADES | SCORE   | DELTA (vs Base) | WIN RATE | SL EXPOSURE (pts) | ENGINE GATE FAILURES (min_rr >= 1)|
+---------------+--------+---------+-----------------+----------+-------------------+-----------------------------------+
| Baseline      | 165    | -47.0   | -               | 30.30%   | 1,200.0           | 0 / 165 (0.00%)                   |
| Round 1 (22)  | 165    | +18.0   | +65.0           | 47.27%   | 1,914.0           | 129 / 165 (78.18%)                |
| Round 2 (29)  | 165    | +45.0   | +92.0           | 53.94%   | 2,204.0           | 165 / 165 (100.00%)               |
| Round 3 (30)  | 165    | +54.0   | +101.0          | 56.36%   | 2,160.0           | 165 / 165 (100.00%)               |
+-------------------------------------------------------------------------------------------------------------------+
```

### Risk & Practical Implementation Verdict
1. **Scoring Mechanism vs. Risk-Adjusted Edge**: Under a fixed target level and a fixed $-1.0$ penalty for any stopped trade, widening the stop distance makes outcomes mechanically non-worsening (a trade can convert from $-1$ to $+1$ or $+2$, but never from positive to negative). Consequently, a positive bootstrap score delta does not establish capital profitability or a risk-adjusted trading edge.
2. **Stop-Distance Exposure Diagnostic**: Total configured stop-distance exposure on stopped trades increases from 1,200.0 points in baseline to 1,914.0 points (SL 22), 2,204.0 points (SL 29), and 2,160.0 points (SL 30). Equal $-1.0$ scoring masks this enlarged stop distance. Note that stop-distance sum is a diagnostic metric, not realized cash P&L or guaranteed maximum loss (gaps can exceed stops).
3. **Engine Risk Gate Compliance (`min_rr_ratio = 1.0`)**: In production `config_profiles.py`, the execution engine rejects trade entries where $\text{T1 distance} / \text{SL distance} < 1.0$. Because recorded exhaustion T1 targets are fixed at 18.0 points (129 trades) and 24.0 points (36 trades):
   - At SL = 22: **78.18% of trades (129/165) fail the gate** ($18 / 22 = 0.818 < 1.0$).
   - At SL = 29: **100.00% of trades (165/165) fail the gate** ($24 / 29 = 0.828 < 1.0$).
   - At SL = 30: **100.00% of trades (165/165) fail the gate** ($24 / 30 = 0.800 < 1.0$).
4. **Recommendation**: Automatic production adoption is not recommended. If wider stops are investigated further, prospective shadow tracking alongside adjusted target distances should be evaluated rather than applying wider stops to fixed targets.

---

## Data Ingestion, Cohort Matching, and Integrity Audit

### 1. Ingested Data & Capture Integrity
- **Dhan 1-Minute Bars**: Fresh historical 1-minute bars for NIFTY Index (`security_id=13`, `IDX_I`) spanning `2026-07-20 00:00:00` to `2026-10-13 00:00:00` (`sha256: b571e27f149eece75039dea108a744a37702f51c7c944990040bdcd267bbcdfc`). Exactly 59 complete trading sessions (09:15 to 15:29 IST), 22,125 regular bars, 0 gaps, 0 duplicates.
- **Supabase Snapshot**: Captured from production read-only tables (`sha256: e73f0008b25f1e9a1bcc187667b5e1aba4f24e3cd3957aeedeb3f63dba24a826`). Contains 166 signals, 165 active trades, and 165 analytics records through October 12, 2026.
- **Future State Leakage Audit**: The DB snapshot includes current trade lifecycle states after entry cutoff. However, no future states or analytics outcomes are used in stop selection or trade scoring.
- **Root Independent Baseline**: Independent reproduction with the prior verified scorer confirms that all 165 baseline scores and exit bars match exactly.

### 2. Cohort Matching & Exclusions
- Total matched trades: **165 trades** exported to `cohort.csv`.
- Matching methods: 42 via `signal_uuid`, 123 via unique `(direction, entry_timestamp)`.
- One unentered signal (`id=505`, `2026-09-24 13:22 IST`) lacked an active trade and was quarantined in `cohort_exclusions.json`.
- **Signal ID 475 Timing Caveat**: Source signal ID 475 matches active legacy trade `signal_id=1892` (`entry_timestamp: 2026-09-17 13:28:00 IST`). The recorded trade stop loss timestamp in the database was `13:28:48.771636 IST`, prior to the first full replay minute at `13:29:00 IST`. This trade is retained in all paired samples (evaluated from first available minute 13:29:00); the risk of omitted sub-minute pre-replay seconds is noted, and the stored outcome is kept for reference only.

### 3. Chronological Partitions
- **Development (Train)**: `2026-07-20` to `2026-08-31` (31 trading sessions, 82 trades).
- **Historical Validation**: `2026-09-01` to `2026-10-12` (28 trading sessions, 83 trades). Historical validation data was previously examined in research; it is not untouched out-of-sample data.

---

## Stop-Loss Selection on Development Only

Stop distances were derived strictly from the 82 Development trades:
1. Each entry was evaluated post-entry for at most 30 minutes (or same session) to first T1 touch, ignoring original SL.
2. Maximum Adverse Excursion (MAE) was measured up to T1 touch (including the adverse extreme of the T1 touch bar as an upper bound).
3. Exactly 8 trades qualified as recoveries reaching T1 with $\text{MAE} > \text{original SL}$ (`developmentMAE.csv`: MAE values `[13.30, 13.60, 18.65, 19.40, 20.85, 29.25, 29.50, 40.65]`).
4. Quantiles on $n=8$: $q_{50} = 20.125$, $q_{75} = 29.3125$, $q_{90} = 32.8450$.
5. Applying formula $\operatorname{clip}(\lceil q \rceil + 1, 13, 30)$:
   - $q_{50} \to 22$.
   - $q_{75} \to \lceil 29.3125 \rceil + 1 = 31 \to$ bounded to 30.
   - $q_{90} \to \lceil 32.8450 \rceil + 1 = 34 \to$ bounded to 30.
   - The $q_{90}$ duplicate was resolved to the nearest unused integer in $[13, 30] \to 29$.
   - Sorted ascending: **[22, 29, 30]**. Frozen in `selection.json`. `selection.py` refuses to overwrite existing selection files and halts if fallback reachers $< 5$.

---

## Detailed Performance by Round

```
=============================================================================================================================
Summary of Performance Across All Variants (165 Recorded Trades)
=============================================================================================================================
Variant   SL Width   T1_BE   T1_Pending   Pending_pre_T1   Total_Unfinished   T2   SL   Total Score   Win Rate%   Exposure (pts)
-----------------------------------------------------------------------------------------------------------------------------
Baseline   10 / 12     31         1              0                 1          18  115      -47.0        30.30%       1,200.0
Round 1      22        50         1              0                 1          27   87      +18.0        47.27%       1,914.0
Round 2      29        56         1              0                 1          32   76      +45.0        53.94%       2,204.0
Round 3      30        59         1              0                 1          33   72      +54.0        56.36%       2,160.0
=============================================================================================================================
```

### Partition Breakdown
- **Development (82 trades, 31 sessions)**:
  - Baseline: Score **-19.0** (15 T1_BE, 0 T1_Pending, 11 T2, 56 SL; WR 31.71%; Exposure 610.0 pts).
  - Round 1 (SL 22): Score **+11.0** (24 T1_BE, 0 T1_Pending, 15 T2, 43 SL; WR 47.56%; Exposure 946.0 pts).
  - Round 2 (SL 29): Score **+23.0** (27 T1_BE, 0 T1_Pending, 17 T2, 38 SL; WR 53.66%; Exposure 1,102.0 pts).
  - Round 3 (SL 30): Score **+30.0** (29 T1_BE, 0 T1_Pending, 18 T2, 35 SL; WR 57.32%; Exposure 1,050.0 pts).
- **Historical Validation (83 trades, 28 sessions)**:
  - Baseline: Score **-28.0** (16 T1_BE, 1 T1_Pending, 7 T2, 59 SL; WR 28.92%; Exposure 590.0 pts).
  - Round 1 (SL 22): Score **+7.0** (26 T1_BE, 1 T1_Pending, 12 T2, 44 SL; WR 46.99%; Exposure 968.0 pts).
  - Round 2 (SL 29): Score **+22.0** (29 T1_BE, 1 T1_Pending, 15 T2, 38 SL; WR 54.22%; Exposure 1,102.0 pts).
  - Round 3 (SL 30): Score **+24.0** (30 T1_BE, 1 T1_Pending, 15 T2, 37 SL; WR 55.42%; Exposure 1,110.0 pts).

### Directional Trajectory
- **BULLISH (92 trades)**: Baseline -27.0 $\to$ Round 1 -1.0 $\to$ Round 2 +17.0 $\to$ Round 3 +19.0.
- **BEARISH (73 trades)**: Baseline -20.0 $\to$ Round 1 +19.0 $\to$ Round 2 +28.0 $\to$ Round 3 +35.0.

---

## Statistical Evaluation: Paired Bootstrap Analysis

Daily block paired bootstrap resampling (seed 42, 2,000 resamples) was executed across all **59 available session dates** (31 Development, 28 Historical Validation). Days with no trades in a block contribute zero delta to daily sums:

```
+-------------------------------------------------------------------------------------------------------------------------+
| CANDIDATE | BLOCK                 | TRADES | SESSIONS | OBS DELTA | BOOT MEAN | 95% BOOTSTRAP CI   | FRAC NONPOSITIVE |
+-----------+-----------------------+--------+----------+-----------+-----------+--------------------+------------------+
| Round 1   | All Sessions          | 165    | 59       | +65.0     | +64.97    | [+42.98, +90.00]   | 0.0000           |
| (SL 22)   | Development           | 82     | 31       | +30.0     | +30.15    | [+13.00, +48.00]   | 0.0000           |
|           | Historical Validation | 83     | 28       | +35.0     | +34.96    | [+20.00, +52.02]   | 0.0000           |
|           | Bullish               | 92     | 59       | +26.0     | +25.99    | [+13.00, +41.00]   | 0.0000           |
|           | Bearish               | 73     | 59       | +39.0     | +38.92    | [+21.00, +59.00]   | 0.0000           |
+-----------+-----------------------+--------+----------+-----------+-----------+--------------------+------------------+
| Round 2   | All Sessions          | 165    | 59       | +92.0     | +92.17    | [+66.00, +120.00]  | 0.0000           |
| (SL 29)   | Development           | 82     | 31       | +42.0     | +42.22    | [+24.00, +62.00]   | 0.0000           |
|           | Historical Validation | 83     | 28       | +50.0     | +50.19    | [+32.00, +69.00]   | 0.0000           |
|           | Bullish               | 92     | 59       | +44.0     | +44.12    | [+26.00, +65.00]   | 0.0000           |
|           | Bearish               | 73     | 59       | +48.0     | +48.16    | [+29.00, +69.02]   | 0.0000           |
+-----------+-----------------------+--------+----------+-----------+-----------+--------------------+------------------+
| Round 3   | All Sessions          | 165    | 59       | +101.0    | +101.09   | [+74.00, +129.00]  | 0.0000           |
| (SL 30)   | Development           | 82     | 31       | +49.0     | +49.56    | [+30.00, +71.02]   | 0.0000           |
|           | Historical Validation | 83     | 28       | +52.0     | +52.22    | [+34.00, +73.00]   | 0.0000           |
|           | Bullish               | 92     | 59       | +46.0     | +45.86    | [+27.00, +68.00]   | 0.0000           |
|           | Bearish               | 73     | 59       | +55.0     | +54.75    | [+35.00, +77.00]   | 0.0000           |
+-------------------------------------------------------------------------------------------------------------------------+
```
*Note: In the multi-day primary replay, all pairs resolved (unresolved pairs excluded: 0 across all blocks). Results are descriptive resample distributions; no formal hypothesis-test or statistical significance claims are made.*

---

## Trade Transitions & Persisting Losers

- **Rescued Trades** (baseline SL converting to T1_BE or T2):
  - Round 1 (SL 22): **28 trades rescued** (19 to T1_BE, 9 to T2).
  - Round 2 (SL 29): **39 trades rescued** (25 to T1_BE, 14 to T2).
  - Round 3 (SL 30): **43 trades rescued** (28 to T1_BE, 15 to T2).
- **Persisting Stop-Loss Trades** (stopped under both baseline and wider SL):
  - Round 1 (SL 22): **87 trades persist as SL** (absorbing 22.0 pts configured stop distance).
  - Round 2 (SL 29): **76 trades persist as SL** (absorbing 29.0 pts configured stop distance).
  - Round 3 (SL 30): **72 trades persist as SL** (absorbing 30.0 pts configured stop distance).
- **Worsened Trades**: **0 trades** scored lower than baseline under the strict scoring rules.

---

## Sensitivity Analysis

Each sensitivity switches outcome and recomputes total stop-loss exposure based on that convention's actual outcomes:

```
+---------------------------------------------------------------------------------------------------------------+
| VARIANT   | CONVENTION               | SCORE   | SL HITS | T1_BE | T2 | UNFINISHED | SL EXPOSURE (pts)        |
+-----------+--------------------------+---------+---------+-------+----+------------+--------------------------+
| Baseline  | Primary Strict           | -47.0   | 115     | 31    | 18 | 1          | 1,200.0                  |
|           | Optimistic Target-First  | -47.0   | 115     | 31    | 18 | 1          | 1,200.0                  |
|           | EOD Censored (Same-Day)  | -52.0   | 114     | 25    | 15 | 11         | 1,190.0                  |
|           | Open Entry Price         | -44.0   | 114     | 31    | 19 | 1          | 1,190.0                  |
+-----------+--------------------------+---------+---------+-------+----+------------+--------------------------+
| Round 1   | Primary Strict           | +18.0   | 87      | 50    | 27 | 1          | 1,914.0                  |
| (SL 22)   | Optimistic Target-First  | +23.0   | 85      | 51    | 28 | 1          | 1,870.0                  |
|           | EOD Censored (Same-Day)  | +12.0   | 79      | 42    | 18 | 26         | 1,738.0                  |
|           | Open Entry Price         | +21.0   | 86      | 50    | 28 | 1          | 1,892.0                  |
+-----------+--------------------------+---------+---------+-------+----+------------+--------------------------+
| Round 2   | Primary Strict           | +45.0   | 76      | 56    | 32 | 1          | 2,204.0                  |
| (SL 29)   | Optimistic Target-First  | +48.0   | 75      | 56    | 33 | 1          | 2,175.0                  |
|           | EOD Censored (Same-Day)  | +32.0   | 69      | 45    | 21 | 30         | 2,001.0                  |
|           | Open Entry Price         | +52.0   | 73      | 58    | 33 | 1          | 2,117.0                  |
+-----------+--------------------------+---------+---------+-------+----+------------+--------------------------+
| Round 3   | Primary Strict           | +54.0   | 72      | 59    | 33 | 1          | 2,160.0                  |
| (SL 30)   | Optimistic Target-First  | +57.0   | 71      | 59    | 34 | 1          | 2,130.0                  |
|           | EOD Censored (Same-Day)  | +41.0   | 65      | 48    | 22 | 30         | 1,950.0                  |
|           | Open Entry Price         | +52.0   | 73      | 58    | 33 | 1          | 2,190.0                  |
+---------------------------------------------------------------------------------------------------------------+
```

*Key Observations*:
- **EOD Horizon**: When evaluated same-day through 15:29 IST, unresolved trades are censored at session end rather than executed at market close. Under EOD censoring, unfinished trades increase to 11 (baseline), 26 (SL 22), and 30 (SL 29/30). Recomputed stop exposure is correspondingly lower (e.g., 1,738.0 pts for SL 22) because same-day non-stopped positions had not yet hit stops.
- **Open Entry Price**: Evaluating entries from the open of the first post-entry bar preserves the directional improvement (+21.0 to +52.0), showing robustness to execution price noise.

---

## Visual Exhibits

1. **`score_vs_risk.png`**: Side-by-side comparison of movement score vs total configured stop-loss point exposure and the 78% to 100% rejection rate under production `min_rr_ratio = 1.0`.
2. **`trade_rescue_case.png`**: Ledger-driven case study of Signal 305 (2026-08-06 BEARISH). Baseline stop (10 pts) was hit at 13:02; wider SL (22 pts) absorbed the pullback, reached T1 at 13:15, and exited at Break-Even (+1.0 score) at 13:16.
3. **`persistent_loss_case.png`**: Ledger-driven case study of Signal 459 (2026-09-10 BEARISH). Baseline stop (10 pts) was hit at 15:13; wider SL (30 pts) held until the 15:29 closing candle before triggering the 30pt stop, receiving identical $-1.0$ score despite 3x configured stop distance.

---

## Verification & Artifacts

All 18 unit and regression tests in `test_sl_experiments.py` pass (`test_results.txt`):
- Stop distance geometry, baseline preservation (10 vs 12), and target distances verified.
- Known open precedence, T1 $\to$ BE (+1), T2 (+2), and pending trade separation verified.
- Input hash verification before replay and refusal to overwrite frozen selection confirmed.
- `cohort.csv` created (165 records) with Signal 475 legacy match documented.
- Paired bootstrap verified over 59 session dates with `fraction_bootstrap_delta_nonpositive`.
- Exit timestamps (`exit_bar_start`, `exit_known_at = start + 1 min`) and excursion upper bounds verified.
- Root independent baseline verification confirmed: all 165 baseline scores and exit bars match exactly.
