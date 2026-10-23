# Research & Indicator Parity Report: 5-Minute Option-Premium ATR TS

**Date:** 2026-10-23
**Status:** Completed Investigation / Gated for Live Activation
**Task ID:** TASK-20261023
**Author:** Software Architect & QA Agents

---

## 1. Executive Summary

This investigation analyzes the mathematical behavior of the **5-minute Option ATR TS** (ATR 5, HHV 10, Multiplier 2.5) on option contracts, specifically examining contract **22550 PE** (Security ID `51333`, Expiry `2026-10-27`) from the 2026-10-23 morning session (Trade #1950).

### Key Findings
1. **Formula Divergence**:
   - **Formula 1 (Candidate / TradingView Ceyhun ATR TS)**:
     $$\text{raw\_line}_i = \max_{k=0..9} \left( \text{High}_{i-k} - 2.5 \times \text{ATR5}_{i-k} \right)$$
     Computes the volatility-discounted high bar-by-bar, then tracks the rolling 10-bar highest value of those stops.
   - **Formula 2 (ARES Legacy Spot ATR)**:
     $$\text{raw\_line}_i = \left( \max_{k=0..9} \text{High}_{i-k} \right) - 2.5 \times \text{ATR5}_i$$
     Subtracts the *current* ATR from the rolling highest high.
   - On Trade #1950, Formula 1 and Formula 2 diverge by up to **₹12.89** when market volatility expands or contracts.
2. **Warmup & Prior-Session Continuity**:
   - Seeding ATR only from the current session open (`09:15 IST`) requires 5 bars (until `09:40`) to initialize ATR(5) and an additional 9 bars (until `10:25`) to populate the 10-bar HHV window.
   - Continuous multi-session calculation (retaining prior-day closing RMA state) allows the indicator to be fully active at market open (`09:15`), shifting raw lines by ₹0.50–₹0.80. This explains the previously observed **₹0.57 discrepancy** against Dia's chart.
3. **Parity Gate Recommendation**:
   - The pure calculation implementation supports both continuous multi-session history and Formula 1.
   - Test fixtures are frozen with exact numerical values.
   - In accordance with the project specification: **the replacement policy is fully implemented with automated unit/integration tests and offline replay, but live production activation remains gated pending chart-level parity sign-off**.

---

## 2. Mathematical Formulations Compared

### 2.1 Formula 1: Rolling Highest of Volatility-Adjusted Highs
```python
# For each 5-minute bar i:
tr_i = max(high_i - low_i, abs(high_i - close_{i-1}), abs(low_i - close_{i-1}))
atr5_i = (atr5_{i-1} * 4.0 + tr_i) / 5.0  # Wilder's Smoothing / RMA
raw_trail_i = high_i - 2.5 * atr5_i
raw_line_i = max(raw_trail_{i-9}, ..., raw_trail_i)  # HHV(10)
```
- **Properties**: Monotonically retains the best volatility cushion calculated at the time each peak occurred. If volatility subsequently spikes, the historical stop anchor is not retrospectively penalized by the wider ATR.

### 2.2 Formula 2: Rolling Highest High Minus Current ATR
```python
highest_high_10 = max(high_{i-9}, ..., high_i)
raw_line_i = highest_high_10 - 2.5 * atr5_i
```
- **Properties**: Punishes historical peaks when current volatility expands, causing the raw line to drop sharply even if price made no lower low.

---

## 3. 20+ Completed Bars Comparison (Trade #1950: 22550 PE, 2026-10-23)

Data source: `ares-20261023-option-51333.json` (Native completed 5-minute bars aggregated from verified Dhan exchange minutes).

| Bar End (IST) | Close | High | Low | ATR(5) | Formula 1 (HHV-of-Trail) | Formula 2 (HHV - ATR) | Difference (F1 - F2) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 09:20 | 75.75 | 78.00 | 55.85 | Warmup | — | — | — |
| 09:25 | 77.90 | 83.60 | 68.85 | Warmup | — | — | — |
| 09:30 | 73.40 | 76.65 | 64.55 | Warmup | — | — | — |
| 09:35 | 87.70 | 89.20 | 70.60 | Warmup | — | — | — |
| 09:40 | 83.65 | 92.55 | 80.35 | 16.21 | Warmup | — | — |
| 09:45 | 84.85 | 85.25 | 69.80 | 16.06 | Warmup | — | — |
| 09:50 | 90.45 | 92.30 | 78.70 | 15.57 | Warmup | — | — |
| 09:55 | 98.60 | 101.65 | 87.10 | 15.36 | Warmup | — | — |
| 10:00 | 109.00 | 113.00 | 94.00 | 16.09 | Warmup | — | — |
| 10:05 | 127.50 | 131.30 | 109.55 | 17.33 | Warmup | — | — |
| 10:10 (T1) | 147.60 | 150.70 | 117.20 | 20.57 | Warmup | — | — |
| 10:15 (T2) | 151.35 | 160.60 | 141.50 | 20.27 | Warmup | — | — |
| 10:20 | 165.25 | 184.00 | 152.75 | 22.75 | Warmup | — | — |
| 10:25 | 152.60 | 169.95 | 147.50 | 22.69 | **127.13** | **127.28** | −0.15 |
| 10:30 | 154.95 | 160.00 | 145.65 | 21.02 | **127.13** | **131.45** | −4.32 |
| 10:35 | 172.30 | 172.30 | 148.00 | 21.68 | **127.13** | **129.81** | −2.68 |
| 10:40 | 170.85 | 176.65 | 155.00 | 21.67 | **127.13** | **129.82** | −2.69 |
| 10:45 | 158.55 | 174.95 | 151.95 | 21.94 | **127.13** | **129.16** | −2.03 |
| 10:50 | 167.50 | 167.50 | 156.20 | 19.81 | **127.13** | **134.48** | −7.35 |
| 10:55 | 169.40 | 177.50 | 165.00 | 18.35 | **131.63** | **138.13** | −6.50 |
| 11:00 | 197.65 | 200.70 | 169.00 | 21.02 | **148.15** | **148.15** | 0.00 |
| 11:05 | 193.45 | 200.00 | 170.15 | 22.78 | **148.15** | **143.74** | +4.42 |
| 11:10 | 175.65 | 193.80 | 173.00 | 22.39 | **148.15** | **144.73** | +3.42 |
| 11:15 | 168.05 | 178.70 | 160.05 | 21.64 | **148.15** | **146.60** | +1.55 |
| 11:20 | 152.90 | 171.30 | 144.85 | 22.60 | **148.15** | **144.19** | +3.96 |
| 11:25 | 161.80 | 169.00 | 150.10 | 21.86 | **148.15** | **146.05** | +2.11 |
| 11:30 | 174.50 | 176.05 | 157.50 | 21.20 | **148.15** | **147.70** | +0.45 |
| 11:35 | 178.15 | 180.75 | 169.05 | 19.30 | **148.15** | **152.45** | −4.30 |
| 11:40 | 173.25 | 182.10 | 169.10 | 18.04 | **148.15** | **155.60** | −7.45 |
| 11:45 | 171.50 | 176.95 | 162.80 | 17.26 | **148.15** | **157.55** | −9.39 |
| 11:50 | 176.15 | 177.55 | 158.45 | 17.63 | **143.04** | **155.93** | −12.89 |
| 11:55 | 178.10 | 181.00 | 167.90 | 16.72 | **139.19** | **151.99** | −12.80 |

---

## 4. Observations & Lifecycle Milestones on #1950

1. **At T1 (10:09 IST)**:
   - Index touches T1 (22,527.10).
   - Under intraday-only warmup, the option ATR line is not ready until 10:25.
   - At 10:25, Formula 1 raw line is **₹127.13**, which is strictly above the original option SL of **₹123.65**.
   - Active Option SL locks at **₹127.13** (rounded to quote tick ₹127.15 or stored full precision).
2. **At T2 (10:15 IST)**:
   - Index touches T2 (22,472.10).
   - 30% additional allocation model-booked (80% cumulative). Score updated to 2.
   - 20% runner allocation remains active.
   - Index stop protection is disabled; Option ATR / Original Option SL floor governs the runner.
3. **Stop Ratchet at 11:00 IST**:
   - High surges to 200.70; Formula 1 raw line ratchets to **₹148.15**.
   - Active Option SL updates: $\text{locked\_stop} = \max(123.65, 127.13, 148.15) = \mathbf{148.15}$.
4. **Subsequent Consolidation (11:05 – 11:55 IST)**:
   - Raw line falls back to 143.04 and 139.19.
   - Ratchet rule: Locked stop **never decreases**, remaining securely locked at **₹148.15**.
   - Closes range between ₹152.90 and ₹178.10; **none close strictly below ₹148.15**.
   - Runner remains open through 11:55 IST with score 2.

---

## 5. Frozen Fixtures for Automated Testing

1. **Option Contract Invariants**:
   - `security_id`: `"51333"`
   - `exchange_segment`: `"NSE_FNO"`
   - `expiry`: `"2026-10-27"`
   - `strike`: `22550`
   - `option_type`: `"PE"`
   - `original_option_entry`: `139.25`
   - `original_option_sl`: `123.65`
2. **Index Invariants**:
   - `entry_price`: `22552.10`
   - `stop_loss`: `22577.10`
   - `target_1`: `22527.10`
   - `target_2`: `22472.10`
3. **Indicator Invariants (Formula 1)**:
   - Bar 10:25 IST: Close `152.60`, Raw Line `127.13` $\rightarrow$ Locked SL `127.13` (Active)
   - Bar 11:00 IST: Close `197.65`, Raw Line `148.15` $\rightarrow$ Locked SL `148.15` (Ratcheted)
   - Bar 11:55 IST: Close `178.10`, Raw Line `139.19` $\rightarrow$ Locked SL `148.15` (Retained)
