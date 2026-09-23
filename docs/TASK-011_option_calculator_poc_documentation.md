# POC Documentation: Option Calculator & Greeks Sizing Overhaul (TASK-011)

**Date:** 2026-09-18  
**Status:** POC Completed & Validated in Scratch Pads  
**Target Module:** `options_math.py` (Option Suggestion & Risk Sizing Layer)  
**Underlying:** NIFTY 50 (`security_id: 13`, `segment: IDX_I`)  

---

## 1. Executive Summary & Objective

The objective of this proof-of-concept (POC) is to evaluate and solve the problem of **capital erosion due to theta decay during consolidation phases**, while retaining the ability to capture explosive moves via gamma when a breakout or trend move triggers.

Initially, shifting contract selection from the **Current Week Expiry** (e.g. 22-Sep-2026) to the **Next Week Expiry** (e.g. 29-Sep-2026) was hypothesized as a way to curb theta bleed. However, an empirical audit using live credentials from the Dhan API revealed that moving to next-week expiry creates severe liquidity and spread penalties.

Following a thorough design grill and audit, the strategy is refined:
1. **Retain Current Week Expiry**: Preserves peak market liquidity, tightest bid-ask spreads, and native user participation.
2. **Overhaul the Option Calculator (`options_math.py`)**:
   - Correct strike selection bias toward OTM contracts (filter $\Delta \ge 0.45$, select closest to ATM $0.50$).
   - Replace linear target estimates with **Gamma-adjusted acceleration**.
   - Transition to **Index-Referenced Synthetic Stop Execution** to eliminate false theta shakeouts.
   - Strictly **NO Time-Stop Loss or Time-Based Exits** (strictly adhering to TASK-108 invariant).
   - Pure defined-risk lot sizing based on index stop distance without penalizing lot count with time buffers.
   - Display **Informational Daily Theta Telemetry** as reference context only.

---

## 2. Dhan API Live Audit Findings

Data was fetched live on **2026-09-18** from the DhanHQ API for both upcoming NIFTY weekly expiries (`2026-09-22` and `2026-09-29`) with Spot at **23,301.55**.

### 2.1 Expiry Comparison Matrix

| Metric | Current Week Expiry (`2026-09-22`) | Next Week Expiry (`2026-09-29`) | Variance / Impact |
| :--- | :--- | :--- | :--- |
| **Days to Expiry (DTE)** | 4 calendar days (2 trading days) | 11 calendar days (7 trading days) | Next week has ~3.5x longer lifespan |
| **ATM Strike (23300 CE) LTP** | ₹105.75 | ₹188.90 | Next week premium is **+78.6% higher** |
| **ATM Open Interest (OI)** | **21,239,530** (21.2M contracts) | **5,101,525** (5.1M contracts) | Current week has **4.16x higher liquidity** |
| **OTM-1 (23350 CE) OI** | **16,934,190** (16.9M contracts) | **720,265** (0.72M contracts) | Current week has **23.5x higher liquidity** |
| **ATM CE Delta ($\Delta$)** | 0.555 | 0.582 | Similar initial directional exposure |
| **ATM CE Gamma ($\Gamma$)** | **0.00175** | **0.00105** | Current week Gamma is **+66.7% higher** |
| **Daily Theta ($\Theta$) Points** | -13.65 pts/day | -10.05 pts/day | Next week saves ~3.6 pts/day in absolute decay |
| **Theta as % of Premium** | **12.91% per day** | **5.32% per day** | Current week bleeds 2.4x faster as % of capital |
| **Implied Volatility (IV)** | 8.82% | 9.06% | Comparable volatility regime |

### 2.2 Pros & Cons: Current Week vs. Next Week

#### Current Week Expiry (`2026-09-22`)
* **Pros:**
  - **Unmatched Liquidity**: Massive open interest ensures zero fill slippage, tight 0.05 tick spreads, and instant market execution.
  - **High Gamma (0.00175)**: Yields explosive percentage ROC on fast 50–100 pt index breakout moves (+60% vs +33%).
  - **Low Capital Requirement per Lot**: ₹105.75 vs ₹188.90 allows flexible fractional lot sizing and higher contract count within safe margin.
* **Cons:**
  - **High Theta Bleed Rate**: Loses ~13% of premium per day during stagnant chop.
  - **Rapid Extrinsic Evaporation**: If index consolidates in afternoon sessions, premium decays even with zero spot movement.

#### Next Week Expiry (`2026-09-29`)
* **Pros:**
  - **Slower Percentage Theta Bleed**: Only ~5.3% of option value decays per day.
  - **Smoother Greeks Profile**: Less sensitive to intraday theta pinches.
* **Cons:**
  - **Liquidity Cliff**: OI drops by 75% to 95% across strikes. Market orders converted to Limit MPP on Dhan face slippage risks.
  - **Lower ROC Efficiency**: A +50 pt index move delivers only +33% gain on premium vs +60% on current week.
  - **Double Capital Requirement**: Requires almost 2x capital to purchase the same contract quantity.

> [!IMPORTANT]
> **Conclusion of Audit:** Moving to next-week expiry solves theta decay by ~3.6 pts/day, but destroys liquidity and doubles the capital requirement. The correct engineering solution is to **keep Current Week Expiry** and **fix the mathematical models in the option calculator**.

---

## 3. Audit of Existing `options_math.py` (V1) Flaws

Detailed inspection of `options_math.py` surfaced key design flaws:

```python
# EXISTING V1 STRIKE SELECTION:
for row in full_chain:
    ...
    if 0.45 <= abs_delta <= 0.55:
        candidates.append((strike, ltp, delta, abs_delta))
candidates.sort(key=lambda x: x[3])  # ASCENDING BY ABS DELTA!
best = candidates[0]
```

1. **Bias Towards OTM (Out-of-the-Money)**:
   - Sorting ascending by absolute delta selects the strike closest to `0.45` (OTM).
   - OTM options have **0 intrinsic value**; 100% of their price is extrinsic time value.
   - During market consolidation, OTM strikes experience the steepest percentage decay and drop off the cliff first.
2. **False Stop-Outs from Option Limit Stops**:
   - If an option stop order is placed on premium, the trade gets prematurely stopped out during normal consolidation while the underlying index is still safely above its structural support.
   - Fix: Transition to **Index-Referenced Synthetic Stop Execution** where exits trigger strictly when the underlying NIFTY index hits `signal.stop_loss`.
3. **Linear Delta Target Estimation (`opt_tp_pts = index_tp_pts * delta`)**:
   - Ignores Gamma acceleration ($\frac{1}{2} \Gamma \Delta S^2$).
   - On a 50 pt move with $\Gamma = 0.00174$, the actual gain is $+25.33$ pts, whereas the calculator predicts only $+23.15$ pts (underestimating profit target by ~9.4%).

---

## 4. The V2 Model Specification & Mathematical Derivations

The revised engine implemented in `scratch/poc_option_calculator_v2.py` resolves these issues:

### 4.1 Strike Selection: $\Delta \ge 0.45$, Nearest to ATM
Contracts with $\Delta \ge 0.45$ are filtered, and sorted by minimum distance to ATM (`abs(abs_delta - 0.50)`). This ensures robust liquidity and balanced delta/gamma dynamics.

### 4.2 Gamma-Adjusted Target Pricing
Using the 2nd-order Taylor series expansion of option pricing:
$$\Delta P_{\text{gain}} = \left(\text{Index\_Pts} \times |\Delta|\right) + \frac{1}{2} \Gamma \left(\text{Index\_Pts}\right)^2$$
$$\text{Option\_TP\_Price} = \text{LTP} + \Delta P_{\text{gain}}$$

### 4.3 Pure Index-Referenced Risk Sizing
$$\text{Opt\_SL\_Loss\_Pts} = \text{Index\_SL\_Pts} \times |\Delta|$$
$$\text{Suggested\_Lots} = \left\lfloor \frac{\text{Capital} \times \text{Risk\_Pct}}{\text{Opt\_SL\_Loss\_Pts} \times \text{Lot\_Size}} \right\rfloor$$
$$\text{Adjusted\_Lots} = \min(\text{Suggested\_Lots}, \; \text{Affordable\_Lots})$$

### 4.4 Invariants Strictly Preserved
- **NO Time-Stop Loss**: Zero timer-based exits or automatic break-even moves (TASK-108 invariant strictly maintained).
- **Index-Referenced Execution**: The trade is monitored and exited strictly based on the underlying NIFTY 50 index crossing `signal.stop_loss` or `signal.target_1`.
- **Informational Telemetry**: Daily theta rate is displayed as a suggestion/reference on Discord and console alerts, but never used to cut lot size or force an early exit.

---

## 5. Live Simulation Comparison: V1 vs. V2

Using live audit data on **23350 CE** (Spot: 23,301.55, LTP: ₹80.45, $\Delta = 0.463$, $\Gamma = 0.00174$, $\Theta = -13.25$, Capital: ₹100,000, Risk: 10%, SL: 25 pts, TP1: 50 pts):

| Parameter | Existing V1 Calculation | Proposed V2 Calculation | Rationale / Benefit |
| :--- | :--- | :--- | :--- |
| **Expected TP1 Gain** | +₹23.15 | **+₹25.33** | Includes +₹2.18 Gamma acceleration boost |
| **Option TP Price** | ₹103.60 | **₹105.78** | Realistically captures non-linear price surge |
| **SL Loss per Share** | ₹11.58 | **₹11.58** | Pure defined risk based on index stop distance |
| **Suggested Lots** | 13 lots | **13 lots** | Unpenalized risk sizing |
| **Total Capital Deployed** | ₹67,980.25 | **₹67,980.25** | Standard allocation |
| **Stop Trigger Source** | Option price threshold | **Index Spot crossing SL** | Zero false theta stop-outs |
| **Informational Theta** | Not displayed | **-13.25 pts/day** | Reference telemetry for trader |

---

## 6. Verification & Test Suite

The POC module and unit tests were built and validated in:
- Implementation: `scratch/poc_option_calculator_v2.py`
- Test Suite: `scratch/test_poc_option_calculator_v2.py`

**Test Results:**
```
Ran 4 tests in 0.000s
OK:
- test_risk_and_points (PASSED)
- test_gamma_adjusted_target (PASSED)
- test_lots_calculation (PASSED)
- test_bearish_calculation (PASSED)
```
