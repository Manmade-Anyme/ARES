# ARES Trading Methodology: Options Greeks Calculation, Dual-Layer Brackets & Dynamic ATR Trailing

- **Status**: Implemented for PR #136 (Greeks & Dual-Layer Brackets); Dynamic ATR Trailing staged as roadmap (`TODO.md`)
- **Author**: Documentation Agent & Software Architect
- **Task Reference**: [MANM-176](https://github.com/Manmade-Anyme/ARES/pull/136), [MANM-264](https://github.com/Manmade-Anyme/ARES/pull/138)
- **Obsidian Vault Suite**: `Trading Methodology/ARES Options & Greeks Methodology/`

---

## 1. Executive Summary

ARES generates high-probability algorithmic trading signals for **NIFTY 50** based on 1-minute microstructure, order flow (OI Wall interaction), and multi-timeframe price action. However, executing these spot signals in live markets requires trading weekly NIFTY options (Call and Put contracts). 

Weekly index options cannot be treated as linear spot proxies. They exhibit strong second-order price curvature (Gamma, $\Gamma$), rapid intraday extrinsic decay (Theta, $\Theta$), implied volatility sensitivity (Vega, $\mathcal{V}$), discrete strike spacing, and tick constraints.

This document serves as the master repository reference for:
1. **Strike Selection & Expiry Policy**: Why ARES strictly retains Current Week Expiry and enforces At-The-Money (ATM) selection ($||\Delta| - 0.50| \to \min$).
2. **2nd-Order Taylor Pricing**: Non-linear price translation ($\Delta P = \Delta \cdot \Delta S + \frac{1}{2}\Gamma(\Delta S)^2$) and conservative risk-loss floors.
3. **Dual-Layer Stop-Loss Model**: Layer 1 (Synthetic Spot Stop to eliminate false theta/IV shakeouts) and Layer 2 (Catastrophic Broker Hard Stop).
4. **Dynamic ATR Trailing Stop**: The 5-minute ATR Trailing Stop architecture, Wilder's RMA, structural HHV/LLV channels, and the High-Water Mark (HWM) Ratchet Invariant.

---

## 2. Quantitative Mathematical Foundations

### 2.1 Expiry Policy: Current Week vs. Next Week
Live Dhan API empirical audits demonstrated:
- **Current Week Expiry**: Peak market liquidity (21.2M vs 5.1M open interest, 4.16× higher), tightest bid-ask spread (0.05 tick), +66.7% higher Gamma (0.00175 vs 0.00105), lower capital per lot (₹105.75 vs ₹188.90).
- **Next Week Expiry**: Suffers a 75% to 95% liquidity cliff across strikes, doubles capital requirements, and widens execution slippage.
- **Rule**: ARES strictly trades **Current Week Expiry**. Theta decay is resolved structurally via underlying synthetic stops rather than sacrificing liquidity.

### 2.2 ATM-Biased Strike Selection
Legacy ascending delta sorts selected OTM options ($|\Delta| \approx 0.45$) possessing zero intrinsic value and steep extrinsic decay.
- **Algorithm**:
  1. Filter contracts where $|\Delta| \ge 0.45$.
  2. Minimize distance to pure ATM:
     $$\text{rank\_key} = \left| |\Delta| - 0.50 \right|$$
  3. Select the candidate minimizing $\text{rank\_key}$.

### 2.3 2nd-Order Taylor Series Derivatives Pricing
For current option entry price $P_0$ at index spot $S_0$:
$$P(S) \approx P_0 + \Delta \cdot (S - S_0) + \frac{1}{2} \Gamma \cdot (S - S_0)^2$$

- **Calls ($\Delta > 0, \Gamma > 0$)**: On favorable upside moves ($\Delta S > 0$), positive gamma curvature accelerates gains by +9.4% to +15% over linear models.
- **Puts ($\Delta < 0, \Gamma > 0$)**: On favorable downside moves ($\Delta S < 0$), $\Delta \cdot \Delta S > 0$ and $\frac{1}{2}\Gamma(\Delta S)^2 > 0$ provide positive convexity.
- **Conservative Risk-Loss Floor**: At spot stop loss ($S_{SL}$), risk sizing enforces:
  $$\text{RiskLossPts} = \max\Big(P_0 - P(S_{SL}),\ |\Delta| \cdot |S_0 - S_{SL}|\Big)$$
  Preventing dangerous over-sizing based on theoretical curvature.

### 2.4 Capital Hard-Borders & Non-Negative Sizing
- If broker available balance $C \le 0$, sizing returns 0 lots with `NO_AVAILABLE_CAPITAL` (eliminating historical `-1 lot` bugs).
- Sizing equations:
  $$\text{suggested\_lots} = \lfloor \frac{C \times r}{\text{RiskLossPts} \times \text{lot\_size}} \rfloor, \quad \text{affordable\_lots} = \lfloor \frac{C}{P_{\text{entry}} \times \text{lot\_size}} \rfloor$$
  $$\text{final\_lots} = \max\Big(0,\ \min(\text{suggested\_lots},\ \text{affordable\_lots},\ \text{max\_lots\_cap})\Big)$$
- Invariant: $\text{final\_lots} \ge 0$ under all circumstances.

### 2.5 Exchange Tick Snapping & Monotonic Level Invariant
- All calculated option levels are snapped to the nearest ₹0.05 NSE tick:
  $$\text{round\_to\_tick}(p) = \text{round}(p / 0.05) \times 0.05$$
- Executable brackets must strictly satisfy:
  $$0.05 \le \text{option\_sl} < \text{entry\_limit} < \text{option\_t1} < \text{option\_t2}$$

---

## 3. Dual-Layer Execution Architecture

```text
┌────────────────────────────────────────────────────────────────────────┐
│                   ARES DUAL-LAYER EXECUTION MODEL                       │
├────────────────────────────────────────────────────────────────────────┤
│ 1. Upfront Limit Entry:                                                │
│    P_limit = min(BestAsk, LTP + slippage_buffer)                       │
│                                                                        │
│ 2. Target 1 (TP1):                                                     │
│    Upfront Limit Order at P_tp1 (Gamma-adjusted target price)          │
│                                                                        │
│ 3. Dual-Layer Stop Loss:                                               │
│    ┌──────────────────────────────────────────────────────────────┐    │
│    │ LAYER 1: Primary Synthetic Stop (Underlying Spot Trigger)    │    │
│    │ • Monitored continuously in ARES engine                      │    │
│    │ • Triggers market/limit exit strictly when NIFTY Spot hits   │    │
│    │   signal.stop_loss                                           │    │
│    │ • Zero false shakeouts from intraday theta decay or IV drop  │    │
│    └──────────────────────────────────────────────────────────────┘    │
│    ┌──────────────────────────────────────────────────────────────┐    │
│    │ LAYER 2: Catastrophic Emergency Hard Stop (Broker Bracket)   │    │
│    │ • Placed upfront on Dhan broker as an OCO/stop-loss leg      │    │
│    │ • Set at a wide volatility buffer (e.g. 1.5x - 2.0x Risk SL) │    │
│    │ • Protects against flash crashes, API drops, or gap events   │    │
│    └──────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────┘
```

- **Layer 1 (Synthetic Spot Stop)**: Evaluated in real time by the ARES Position Manager. Exit order emitted strictly when spot touches `signal.stop_loss`. Zero false shakeouts from option wicks or theta decay.
- **Layer 2 (Emergency Hard Stop)**: Placed upfront at the broker at a wide volatility buffer ($1.5\times$ to $2.0\times$ risk) as an exchange circuit-breaker.
- **No Time-Stops (TASK-108)**: Strictly no automatic clock-time or candle-count expirations.

---

## 4. Dynamic 5-Minute ATR Trailing Stop (Roadmap)

### 4.1 Two-Phase Lifecycle
1. **Phase 1 (Entry Invalidation Gate)**: Retains tight, setup-specific structural spot stops (e.g. 10 pts for Exhaustion) to guarantee $R:R \ge 1.8$ and pass the engine's `min_rr_ratio = 1.0` gate.
2. **Phase 2 (HWM Ratchet Trailing)**: Activates after Target 1 ($T_1$) is reached (+18 pts Spot). 50% lots are closed, stop moves to Cost (Spot Entry), and dynamic trailing begins on runners.

### 4.2 Mathematical Formulas
- **Timeframe**: `5m` NIFTY Spot candles.
- **Wilder's Smoothing ($N=5$)**:
  $$\text{TR}_t = \max(H_t - L_t, |H_t - C_{t-1}|, |L_t - C_{t-1}|)$$
  $$\text{ATR}_5(t) = \frac{4 \times \text{ATR}_5(t-1) + \text{TR}_t}{5}$$
- **Structural Channel ($M=10$)**:
  $$\text{HHV}_{10}(t) = \max(H_t, \dots, H_{t-9}), \quad \text{LLV}_{10}(t) = \min(L_t, \dots, L_{t-9})$$
- **Dynamic Bands**:
  - Bullish (CE): $\text{ATR\_TS}_{\text{spot}} = \text{HHV}_{10} - (2.5 \times \text{ATR}_5)$
  - Bearish (PE): $\text{ATR\_TS}_{\text{spot}} = \text{LLV}_{10} + (2.5 \times \text{ATR}_5)$

### 4.3 High-Water Mark (HWM) Ratchet Invariant
- **Bullish (CE)**: Monotonically non-decreasing:
  $$\text{SL}_t = \max\Big(\text{SL}_{t-1},\quad \text{Spot Entry},\quad \text{ATR\_TS}_{\text{spot}}(t)\Big)$$
- **Bearish (PE)**: Monotonically non-increasing:
  $$\text{SL}_t = \min\Big(\text{SL}_{t-1},\quad \text{Spot Entry},\quad \text{ATR\_TS}_{\text{spot}}(t)\Big)$$
- **"Never Surrender Ground" Invariant**: The stop never retreats on ATR contractions or temporary consolidation pullbacks.
- **Break-Even Floor Guarantee**: Stop on runners cannot drop below initial entry cost once $T_1$ is hit.

---

## 5. Verification & Changelog Links

- **PR #136 Implementation**: Merged as commit `428a9d5f074515b760757295880a73880c451a1d`.
- **Coverage Gate**: 100% statement and branch coverage across `options_math.py`, `execution/order_manager.py`, and `execution/signal_dispatch.py`.
- **Obsidian Knowledge Vault**:
  - `Trading Methodology/ARES Options & Greeks Methodology/ARES Options Methodology Index.md`
  - `Trading Methodology/ARES Options & Greeks Methodology/Options Greeks Calculation & Bracket Execution.md`
  - `Trading Methodology/ARES Options & Greeks Methodology/Dynamic ATR Trailing Stop & High-Water Mark Invariant.md`
  - `Projects/Ares/ADR-176_options-entry-exit-sl-greeks-limit-orders.md`
  - `Projects/Ares/MANM-176-log.md`
