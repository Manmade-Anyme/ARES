# POC Trend Expansion & Structural Reversal Strategy Validation

**Author:** Multica Debug & Architecture Agent  
**Date:** 2026-11-05  
**Data Sources:** Dhan API (1-minute real tick data) & Supabase Telemetry  
**Obsidian Cross-References:** `[[2026-11-05]]`, `[[2026-11-03]]`, `[[2026-10-30]]`, `[[2026-10-29]]`  
**Code Reference:** [`poc/trend_expansion_poc.py`](file:///Users/manmadeanyme/Documents/Work/ARES/poc/trend_expansion_poc.py) | Runner: [`poc/run_trend_expansion_backtest.py`](file:///Users/manmadeanyme/Documents/Work/ARES/poc/run_trend_expansion_backtest.py)

---

## 1. Executive Summary

ARES's core strategy suite (`TREND_CONTINUATION`, `FAILED_BREAKOUT`, `EXHAUSTION_REVERSAL`) is heavily optimized for mean-reverting ranges and shallow pullbacks. On **trend expansion / short-squeeze days** (such as 2026-11-05, 2026-11-03, and 2026-10-29), ARES either:
1. Gets stopped out on premature continuation entries during pre-expansion liquidity sweeps (-25 pts today).
2. Remains 100% dormant because pullbacks never touch VWAP within 10 points (0 trades on 2026-11-03's +370 pt vertical trend).
3. Takes counter-trend exhaustion traps attempting to fade runaway momentum (-50.8 pts on 2026-10-30).

We designed and backtested a specialized **Trend Expansion POC Strategy** incorporating two complementary setups:
1. **`AFTERNOON_BREAKOUT` (Session High / Range Expansion Breakout):**
   - Monitors multi-hour range accumulation (09:15 to 12:30 IST).
   - Enters between 12:30 and 14:15 IST when price breaks out of the morning range with $\text{Close} > \text{VWAP}$, $\text{Close} > \text{EMA}_{20}$, and volume participation.
   - Leverages options call-writer short-covering cascades.
2. **`VWAP_RECLAIM` (Structural Shakeout & Higher-Low Reversal):**
   - Requires a $\ge 60$ pt morning impulse.
   - Waits for a 35%–68% Fibonacci liquidity sweep (38.2% to 66.7% golden ratio zone) below VWAP that respects the day low.
   - Enters on a verified VWAP reclaim candle with $\text{EMA}_9 \ge \text{EMA}_{20}$.

---

## 2. Comparative Performance: ARES Live vs POC Strategy

**Historical results note:** The comparative table and 2026-11-05 trade figures below were produced by an earlier POC lifecycle and have not been rerun with the conservative OHLC ordering documented in section 6. Treat them as historical spot-point backtest outputs, not validation of the current lifecycle or evidence of executed trades. Option values are reference marks only.

Evaluated on 7 consecutive sessions documented in the Obsidian Daily Market Observation notes:

| Date | NIFTY Range (Net Move) | Obsidian ARES PnL | POC Strategy PnL | Delta (Outperformance) | Market Regime |
|---|---|---|---|---|---|
| **2026-10-28** | 280.3 pts (+20.8 pts) | +20.11 pts | **-33.00 pts** | -53.11 pts | Choppy morning $\rightarrow$ late bounce |
| **2026-10-29** | 305.2 pts (+246.1 pts) | +84.20 pts | **+63.85 pts** | -20.35 pts | Dominant Bullish Expansion (+246 pts) |
| **2026-10-30** | 213.0 pts (+88.6 pts) | -50.80 pts | **+0.00 pts** | **+50.80 pts** | Choppy Friday reversal traps (POC preserved capital) |
| **2026-11-02** | 233.8 pts (+218.5 pts) | +4.75 pts | **+2.00 pts** | -2.75 pts | Strong Bullish Expansion |
| **2026-11-03** | 328.0 pts (+228.6 pts) | +0.00 pts | **-33.40 pts** | -33.40 pts | Vertical +370 pt trend (ARES 0 trades) |
| **2026-11-04** | 264.4 pts (+218.3 pts) | -15.15 pts | **+0.00 pts** | **+15.15 pts** | Pre-expiry range chop (POC preserved capital) |
| **2026-11-05** | 283.6 pts (+158.5 pts) | -25.00 pts | **+122.15 pts** | **+147.15 pts** | Trend reversal + PM squeeze (Today) |
| **TOTAL** | | **+18.11 pts** | **+121.60 pts** | **+103.49 pts (+571%)** | |

---

## 3. Deep Dive on Today's Trades (2026-11-05)

### Trade 1: `VWAP_RECLAIM`
- **Entry:** 11:02 IST @ **24,372.20**
- **Stop Loss:** 24,341.90 (30.3 pts risk, placed beneath the 10:44 flush low of 24,344.90).
- **Target 1:** 24,442.20 (Morning high reached at 12:36 IST, ratcheted SL to BE + 2 pts).
- **Exit:** 13:55 IST @ **24,529.35** on 20 EMA trailing break.
- **Spot PnL:** **+157.15 pts**
- **Option-reference marks (24400 CE):** Approximate entry mark ~138.00 $\rightarrow$ approximate exit mark ~250.00 (**+112.00 premium points / +81.2% reference-mark change**). These are hypothetical market-reference marks only: they are not executed fills, do not assert that either price was executable, and are not realized or after-cost account P&L. The strategy's reported result is a spot-point backtest output.

### Trade 2: `AFTERNOON_BREAKOUT`
- **Entry:** 14:05 IST @ **24,563.90**
- **Stop Loss:** 24,528.90 (-35.0 pts).
- **Exit:** 14:19 IST @ 24,528.90 (`SL_HIT`).
- **Spot PnL:** -35.00 pts.
- **Net Session PnL:** **+122.15 pts** (compared to ARES **-25.00 pts**).

---

## 4. Key Engineering Architecture in the POC

```
[09:15 - 10:15] Anchor Morning Impulse & Range High/Low
      │
      ├── [10:30 - 12:30] Liquidity Sweep & VWAP Reclaim Setup
      │     ├─ Deep Fib retrace (40-65%) below VWAP?
      │     ├─ Low holds above session low (Higher Low)?
      │     └─ Clean close > VWAP with EMA9 >= EMA20 -> ENTER VALUE REVERSAL
      │
      └── [12:30 - 14:15] Afternoon Range Expansion Breakout Setup
            ├─ Multi-hour range (09:15 - 12:30) established?
            ├─ 1-min close > Range High + 2 pts?
            ├─ Trend aligned (Close > VWAP, Close > EMA20)?
            └─ Volume >= 1.0x SMA20(Vol) -> ENTER MOMENTUM BREAKOUT
                  │
                  ▼
         [Execution & Risk Management]
            ├─ Initial SL: Base swing low (max 35 pts)
            ├─ T1 (+35 pts): Ratchet SL to Breakeven + 2 pts
            ├─ Trailing Runner: Trail 20 EMA after 13:30 IST
            └─ Hard Cutoff: 15:15 IST EOD Close
```

---

## 5. Live Signal Integration (PR #157)

The gated strategy is now wired into `AresEngine` as the fifth live signal strategy. It evaluates completed one-minute candles incrementally, after the existing failed-breakout and OI-wall priority checks and before trend continuation/exhaustion. The regime gate uses the defaults documented in section 6 and requires at least two of four factors.

The live detector emits manual `SIGNAL_REFERENCE` candidates through ARES's existing option-reference, Supabase tracking, and Discord-alert path. It does **not** submit paper or broker entry orders. No Supabase schema migration is required: signal setup types are stored as text, and ML detector-score keys are generated from `SetupType`.

The regime gate requires the previous trading day's close (PDC). The detector also requires an uninterrupted stream of completed current-session candles starting at 09:15 IST; missing PDC, a late process start, or a gap in that stream keeps it dormant for the session. This avoids reconstructing intraday state from bars that the production engine intentionally does not replay after startup.

The live signals preserve the POC's dynamic geometry: reclaim SL is set below the tracked sweep and T1 is the morning high; afternoon-breakout SL uses the recent base within the configured maximum risk and T1 is the configured point reward. T2 is a structural/reference target, with a configured fallback. Both profiles enable the strategy by default; `trend_expansion_enabled` is the explicit off-switch.

These are live signal rules, not a claim that the historical performance table validates current production outcomes. The historical results above were not rerun under the conservative lifecycle, and live integration does not itself establish profitability or option fills.

---

## 6. PR #157 Review Hardening & Edge-Case Audit (2026-11-07)

- **Annotation Import Safety:** Added `from typing import Dict, List, Optional` in audit modules to eliminate runtime `NameError` during annotation introspection.
- **Volume Confirmation:** Enforced `volume >= min_volume_ratio * vol_sma20` (default 1.0) on raw afternoon breakouts to reject low-participation chop.
- **Exact Session VWAP Dispersion:** Corrected cumulative variance calculation to evaluate deviations around current session VWAP mean ($E[TP^2] - (E[TP])^2$).
- **BBW Expansion Gate:** Enforced `bbw_expansion_ratio >= min_bbw_expansion` (default 1.0) before triggering gated afternoon breakouts.
- **Fibonacci Retracement Gating:** Bounded pullback depth to `[min_retrace_ratio, max_retrace_ratio]` (35% to 68%), enforcing upper and lower limits on raw and gated reclaims.
- **State Invalidation & Isolation:** Reset `sweep_low` and `dipped_below_vwap` on trade entry, SL hit, trailing exit, and if price flushes beneath morning low.
- **Audit Context & Date Isolation:** Bounded `load_all_cached_sessions` and `load_cached_sessions_and_pdl` strictly to `[start_date, end_date]`, ensuring prior-day context files do not leak into ARES benchmark calculations.
- **Division-by-Zero Hardening:** Guarded against empty trade sets in audit summary calculations.
- **Configurable regime thresholds:** Added `RegimeThresholds` as the gated strategy's focused settings object. Defaults preserve existing behavior: absolute overnight gap from PDC ≥ 70 spot points; 09:15–09:45 opening-range width ≥ 85 points; cumulative VWAP standard deviation by 10:30 ≥ 25 points; and absolute opening-to-09:45 close displacement ≥ 45 points. Each threshold can be overridden independently.
- **Shared position lifecycle:** Raw and gated engines now use the same helper for stop, T1 stop ratchet, EMA20 trailing exit, EOD close, cooldown, and setup-state reset behavior.
- **Conservative OHLC ordering:** On each completed one-minute candle, the stop that was active before that candle is checked before T1. A T1-triggered stop ratchet becomes effective on the next candle; the same candle's high and low are not used to claim that T1 occurred before a retracement through the new stop. Thus ambiguous candles do not produce an unsupported same-candle stop fill.
- **Live detector integration:** PR #157 adds the gated strategy as an enabled-by-default fifth detector using incremental completed-candle state. It requires verified PDC and a continuous session stream from 09:15 IST, emits manual signal references only, and preserves dynamic POC SL/T1 geometry. No broker-entry routing or database migration is included.
- **Runtime tuning:** Both profiles default the regime factors to gap ≥70 pts, opening range ≥85 pts, VWAP dispersion ≥25 pts, and directional displacement ≥45 pts, with two factors required. Setup defaults also retain the POC geometry: 09:15–12:30 range anchor, 14:15 entry cutoff, 70-pt minimum impulse, 35%–68% reclaim retracement, 2-pt VWAP sweep buffer, 3-pt reclaim stop buffer, 15–35 pt reclaim risk, 12–35 pt breakout risk, 30-pt PDL safety buffer, 35-pt breakout T1, ≥0.95x volume, ≥1.0 BBW expansion, and afternoon VWAP z-score ≥0.8. Each is explicitly configurable in `TuningConfig`.
