# Debug Report: Re-evaluating Directional Performance Asymmetry with New Supabase Data

- **Issue**: MANM-159 (Investigate and resolve directional performance asymmetry)
- **Date**: 2026-10-21
- **Agent**: Debug Agent (`04a73f4c-6895-420c-b9ee-c769307d2805`)
- **Dataset Scope**: Full population from Supabase `trade_analytics` (359 trades: 2026-07-20 to 2026-10-16)

---

## 1. Executive Summary

Following the user directive to re-debug directional asymmetry based on newly recorded live trading data in Supabase, we analyzed the incremental 126 trades closed between 2026-09-15 and 2026-10-16 against the original 233-trade baseline (2026-07-20 to 2026-09-11).

### Key Findings:
1. **Bullish PnL Has Fully Rebounded**: In the new data, Bullish trades generated **+452.85 points** across 71 trades (+6.38 pts/trade, 38.0% win rate, Profit Factor 1.75). The historical deficit of -176.05 pts has been completely erased. All-time Bullish PnL is now **+276.80 points**.
2. **October Performance Reversal**: During October (Weeks 40–42, Sep 28 – Oct 16), **Bullish substantially outperformed Bearish**:
   - **Bullish**: 41 trades, **+399.95 pts** (+9.75 pts/trade, 43.9% win rate, Profit Factor 2.33).
   - **Bearish**: 30 trades, **+184.00 pts** (+6.13 pts/trade, 43.3% win rate, Profit Factor 1.68).
   - Bullish generated more than 2x the net points of Bearish in October.
3. **Macro Regime, Not Signal Logic, Governs Asymmetry**:
   - The original underperformance occurred during an uninterrupted -833 pt Nifty decline (24,181 -> 23,348).
   - In consolidation and rebound regimes (22,260 -> 23,125), Bullish signals demonstrated strong follow-through and positive expectancy.
4. **Bearish Outlier Dependency**:
   - While Bearish yielded +758.15 pts in the new dataset, **81.9% of that profit (+621.15 pts)** came from a single 2-day macro waterfall on September 23–24 (Week 39).
   - Excluding those two outlier days, Bullish outperformed Bearish **+417.85 pts vs +137.00 pts** (a 3:1 advantage for Bullish).
5. **BE-after-T1 Accounting Impact (MANM-66)**:
   - Proper credit of locked entry-to-T1 gains (+18 to +25 pts) in `STOPPED_OUT_AT_BE` contributed +383.00 pts to Bullish and +275.00 pts to Bearish, removing the artificial loss penalty historically imposed when runners returned to entry.

---

## 2. Multi-Period Statistical Comparison

### Table 1: Performance by Time Window

| Dataset Period | Direction | Trades (N) | Total PnL (pts) | Mean (pts/trade) | 95% Bootstrap CI | Win Rate (PnL>0) | Score WR (>=1) | Profit Factor |
|---|---|---|---|---|---|---|---|---|
| **Period 1: Historical**<br>(2026-07-20 to 2026-09-11) | **BULLISH** | 106 | -176.05 | -1.66 | [-5.71, +2.89] | 13.2% | 31.1% | 0.79 |
| | **BEARISH** | 127 | +446.90 | +3.52 | [-1.94, +9.61] | 18.9% | 33.1% | 1.44 |
| **Period 2: Waterfall**<br>(2026-09-15 to 2026-09-25) | **BULLISH** | 30 | +52.90 | +1.76 | [-7.64, +12.42] | 30.0% | 30.0% | 1.18 |
| | **BEARISH** | 25 | +574.15 | +22.97 | [+5.68, +42.86] | 52.0% | 52.0% | 4.15 |
| **Period 3: October Recovery**<br>(2026-09-28 to 2026-10-16) | **BULLISH** | 41 | **+399.95** | **+9.75** | [+0.39, +20.17] | **43.9%** | **43.9%** | **2.33** |
| | **BEARISH** | 30 | +184.00 | +6.13 | [-4.30, +17.80] | 43.3% | 43.3% | 1.68 |
| **New Data Aggregate**<br>(2026-09-15 to 2026-10-16) | **BULLISH** | 71 | **+452.85** | **+6.38** | [-0.48, +13.68] | **38.0%** | **38.0%** | **1.75** |
| | **BEARISH** | 55 | +758.15 | +13.78 | [+3.47, +25.04] | 47.3% | 47.3% | 2.67 |
| **All-Time Population**<br>(2026-07-20 to 2026-10-16) | **BULLISH** | 177 | **+276.80** | **+1.56** | [-2.22, +5.66] | 23.2% | 33.9% | 1.19 |
| | **BEARISH** | 182 | +1205.05 | +6.62 | [+1.47, +12.29] | 27.5% | 37.4% | 1.81 |

---

## 3. Setup-Level Breakdown in New Data (126 Trades)

### Bullish Trades (N=71):
- **`TREND_CONTINUATION`** (N=23): **+253.60 pts** (+11.03 pts/trade, 56.5% WR). Major improvement over the historical -65.00 pts.
- **`EXHAUSTION_REVERSAL`** (N=43): **+139.30 pts** (+3.24 pts/trade, 27.9% WR). Highly reliable baseline contributor.
- **`OI_WALL_REJECTION`** (N=3): **+83.95 pts** (+27.98 pts/trade, 66.7% WR). Low sample size, but positive.
- **`FAILED_BREAKOUT`** (N=2): **-24.00 pts** (-12.00 pts/trade, 0.0% WR). Continues to be unprofitable.

### Bearish Trades (N=55):
- **`TREND_CONTINUATION`** (N=22): **+620.15 pts** (+28.19 pts/trade, 54.5% WR). Accelerated by the Sep 23-24 trend cascade.
- **`EXHAUSTION_REVERSAL`** (N=27): **+182.00 pts** (+6.74 pts/trade, 48.1% WR).
- **`FAILED_BREAKOUT`** (N=5): **-28.00 pts** (-5.60 pts/trade, 20.0% WR). Negative expectancy.
- **`OI_WALL_REJECTION`** (N=1): **-16.00 pts** (-16.00 pts/trade, 0.0% WR).

### Cumulative Setup Observations:
- **`EXHAUSTION_REVERSAL` is symmetrically profitable**: All-time Bullish +186.45 pts vs Bearish +227.75 pts.
- **`FAILED_BREAKOUT` is universally toxic**: All-time -79.00 pts Bullish and -24.55 pts Bearish (-103.55 pts total across 24 trades, 12.5% WR).
- **`TREND_CONTINUATION` requires momentum**: Highly profitable in both directions when the market trends (+188.60 pts all-time Bullish, +1057.85 pts all-time Bearish).

---

## 4. Option Greeks & Execution Audit

Using captured `market_context.options_sizing` for the 126 new trades:
- **Call (CE) Options**: Mean Delta = `0.4926`, Mean Entry Premium = `₹98.22`.
- **Put (PE) Options**: Mean Delta = `0.4864`, Mean Entry Premium = `₹107.97`.

The delta values are identical to ATM theoretical expectations (~0.49). The ~₹10 premium delta between puts and calls represents standard index put skew and does not impede trade expectancy or fill efficiency.

---

## 5. Architectural Conclusions & Recommendations

1. **Vindication of September Reversion**:
   The decision to revert aggressive asymmetric target caps (T2=50 for Bullish vs T2=80 for Bearish) in commit `df6435f` was validated by subsequent market behavior. In October, Bullish trades hit multiple 80-pt and 115.9-pt runners. Artificially truncating bullish targets would have reduced October net gains by ~95 points.
2. **True Structural Weakness is `FAILED_BREAKOUT`**:
   The primary source of drag across the entire system is `FAILED_BREAKOUT` (-103.55 pts net loss, 12.5% win rate). It should be disabled or have its gating tightened across both directions.
3. **Regime-Aware Macro Gating**:
   Rather than handicapping bullish target geometry, the only justified intervention is suppressing counter-trend entries (specifically when a severe macro waterfall is detected on daily/4-hour timeframes).
