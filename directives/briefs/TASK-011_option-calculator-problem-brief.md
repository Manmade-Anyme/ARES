# Problem Brief — TASK-011: Option Calculator & Strike Selection Overhaul

**Date:** 2026-09-18  
**Status:** approved  

## Problem Statement (Original)
"So what I am planning to do now in this project is that I want to do a POC where we want to now shift the expiry day selection from current week expiry to the next week expiry...
So I think the option calculator, option entry, exit and target is the one that needs to be targeted here. The selection of the option over here is one thing that I think is going to change. I forgot to tell you this point: the logic behind the changes is not supposed to be changed because that's where most of the configurations are set. The current week expiry is always the one with the most active users, and the objective of this exercise is to see the option suggestion given that module. I'm trying to figure out the calculation happening around it, which is not the one I want. Let it use the current week expiry because that's where the maximum amount of volumes are. Also dont make any code changes yet, you can change code in scratch pad files.
Make sure we are not adding any time stop loss or anything. I don't want to add that. That was added before and then we got it removed. Just like a suggestion you can add it but no calculations to suggest lot or make a hard exit which should not happen that way."

## Problem Statement (Simplified)
Retain current-week weekly expiry for maximum liquidity and user participation, but overhaul `options_math.py` calculations to accurately capture gamma acceleration on targets, prevent false theta shakeouts via index-referenced execution, preserve unpenalized risk-based lot sizing (strictly zero time-stop loss), and provide informational daily theta telemetry.

## Invariants & Design Decisions Confirmed
1. **Expiry Selection**: Keep Current Week Expiry (`2026-09-22`). 21.2M OI vs 5.1M OI next week guarantees tight spreads and instant execution.
2. **Strike Selection**: Filter $\Delta \ge 0.45$, select strike closest to ATM (Delta 0.50).
3. **Exit Execution**: Strict Index-Referenced Stop & Target. Exits occur only when NIFTY spot hits `signal.stop_loss` or `signal.target_1`. Option SL is an estimated guide, never a hard broker limit on premium.
4. **Target Calculation**: Gamma-adjusted: $\Delta P_{\text{gain}} = (\text{Index\_Pts} \times \Delta) + \frac{1}{2} \Gamma (\text{Index\_Pts})^2$.
5. **Lot Sizing**: Pure risk-based sizing: $\text{Loss\_Per\_Share} = \text{Index\_SL\_Pts} \times \Delta$. Zero time-decay buffer in sizing.
6. **Time-Stop Invariant**: Strictly NO time stop-loss, timer exits, or automatic BE moves (TASK-108 preserved). Daily theta displayed as informational suggestion only.

## Test Validation
- Validated via `scratch/poc_option_calculator_v2.py` and `scratch/test_poc_option_calculator_v2.py` (4 unit tests passing).
