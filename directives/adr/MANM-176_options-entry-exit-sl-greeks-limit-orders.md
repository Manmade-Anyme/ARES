# ADR-176: Comprehensive Options Entry, Exit, and SL Calculations Using Greeks, Index-Referenced Stops, and Upfront Limit Orders

- **Status**: Proposed
- **Date**: 2026-10-15
- **Task ID**: MANM-176
- **Author**: Software Architect Agent
- **Target Branch**: `feature/MANM-176-options-entry-exit-sl`
- **Knowledge Sources & Obsidian Cross-References**:
  - `Projects/Ares/TASK-011 Option Calculator & Greeks Audit POC.md` (Current vs Next Expiry audit, OTM strike bias elimination, Gamma pricing, Index-Referenced Synthetic Stop)
  - `Notes/Daily Market Observation/2026-10-13 - Exhaustion Stop Loss Replay - Three Iterations.md` (165 production trades audit, R:R gate constraint with widened stops)
  - `Projects/Ares/TASK-185_SL-Target-Optimization.md` (Per-type levels table, fixed T1/T2 vs structural runner)
  - `Projects/Delta based strategy/Delta based strategy doc.md` (Underlying structural stop vs premium stop, scaled booking, Tuesday expiry decay rules)
  - `Knowledge/Expected Move — Formula and Straddle Cheat Code.md` (ATM straddle × 1.25 for model-free 1σ move, IV sanity cross-check)
  - `Projects/Kronos/ADRs/ADR-008-dynamic-stop-loss.md` & `ADR-019-stepped-sl-schedule.md` (Monotonic decay guard `min(previous, new)`)

---

## 1. Problem Statement & Root Cause Analysis

### 1.1 Linear Delta-Only Premium Distortion
In the current ARES options calculation layer (`options_math.py:13-14` and `options_math.py:125-127`), premium translation from underlying NIFTY spot points to option premium is strictly linear:
$$\text{opt\_sl\_pts} = \text{index\_sl\_pts} \times |\Delta|$$
$$\text{opt\_tp\_pts} = \text{index\_tp\_pts} \times |\Delta|$$

Option contracts are non-linear derivatives whose price changes with respect to the underlying spot are governed by second-order curvature (Gamma, $\Gamma$), time decay (Theta, $\Theta$), and implied volatility shifts (Vega, $\mathcal{V}$).
- Over index movements of 15 to 50 spot points (standard SL and T1/T2 distances in `config_profiles.py`), ignoring Gamma systematically under-projects target reward for winning long options by **9.4% to 15%** (e.g., on a 50 pt move with $\Gamma = 0.00174$, actual premium gain is +25.33 pts vs +23.15 pts predicted linearly).
- Intraday time decay ($\Theta$) accelerates significantly on expiry days (Tuesday weekly NIFTY expiry), yet no time-horizon or volatility decay discount is applied.

### 1.2 Strike Selection Bias Towards OTM Extrinsic Traps
The current V1 strike selection in `options_math.py:77-83` filters for absolute delta in $[0.45, 0.55]$ and sorts ascending:
```python
candidates.sort(key=lambda x: x[3])  # Ascending by abs_delta!
```
As proven in `TASK-011 Option Calculator & Greeks Audit POC.md`, sorting ascending systematically selects the strike closest to `0.45` (Out-Of-The-Money / OTM). OTM options possess **zero intrinsic value** and consist entirely of extrinsic time value. During intraday consolidation, OTM strikes experience the steepest percentage theta bleed, collapsing in value even when underlying spot holds support.

### 1.3 False Stop-Outs from Hard Option Premium Stops
Placing fixed stop-loss orders directly on option premium causes severe false stop-outs. During market chop or consolidation, extrinsic premium decays (theta) and implied volatility compresses, triggering a premium stop while the underlying NIFTY index is still safely above its structural support zone.
Historical architecture documentation (`Delta based strategy doc.md`, Section 10) explicitly mandates:
> **"SL type: Structural (underlying points), not premium."**

### 1.4 Fabricated Quotes and Inverted Order Levels
When Dhan option chain data lacks `delta` or `ltp` for a candidate strike, `options_math.py:98-112` falls back to synthetic sentinel values: `delta = 0.50` (or `-0.50`) and `LTP = 100.0`. These fabricated numbers are treated as real quotes, resulting in phantom sizing and misleading alerts.
Furthermore, applying an arbitrary Rs 1.0 floor via `opt_sl_price = max(1.0, ltp - opt_sl_pts)` causes critical price inversion whenever $LTP < \text{opt\_sl\_pts}$: a contract trading at Rs 0.50 with a 4-point SL produces an option stop loss of Rs 1.0 (above the entry price).

### 1.5 Negative Available Capital & Inverted Lot Sizing (Production Audit Defect)
As documented in the 2026-09-22 and 2026-09-23 production audits on MANM-176 (35 historical occurrences):
- `fetch_dhan_capital()` returns negative balance if the trading account balance is in debit (e.g., `-₹39.19`).
- `calculate_risk_amount()` propagates the negative rupee amount.
- `calculate_lots()` and `calculate_affordable_lots()` both invoke `math.floor()`, converting `-39.19 / (sl_points * lot_size)` into `-1` suggested lot.
- Downstream reporting multiplies by `-1`, inverting P&L attribution and corrupting analytics tables.

### 1.6 Execution Gap: Reactive Market Orders vs Upfront Limit Orders
ARES currently relies on simulated or reactive spot-level triggers. When trading live option contracts, sending unconstrained market orders incurs severe slippage on NIFTY options (often 2 to 5 premium points in volatile markets). Upfront Limit Orders with predetermined price boundaries are required before order placement.

---

## 2. Decision and Architecture Overview

We decouple and upgrade the options calculation and execution pipeline into five robust architectural pillars:

```mermaid
flowchart TD
    subgraph Market Ingestion & Contract Selection
        A[Engine Tick: AresSignal] --> B[Dhan Option Chain Snapshot]
        B --> C[Contract Selection: Retain Current Week Expiry]
        C --> D[Filter Abs Delta >= 0.45, Select Closest to ATM 0.50]
    end

    subgraph Greeks Math & Validation [options_math.py]
        D --> E{Capital & Quote Valid?}
        E -->|No / Capital <= 0| F[Set lots=0, Flag UNAVAILABLE / NO_CAPITAL, Suppress Order]
        E -->|Yes| G[2nd-Order Taylor Expansion: Delta + Gamma]
        G --> H[Theta & Vega Horizon Scenario Adjustment]
        H --> I[Validate Monotonic Ordering: 0 < SL < Entry < T1 < T2]
        I -->|Ordering Invalid| F
        I -->|Valid| J[Compute Non-Negative Sizing Lots & Tick Rounding 0.05]
    end

    subgraph Dual-Layer Execution Model [order_manager.py]
        J --> K[Layer 1: Upfront Limit Entry: min(Ask, LTP + slippage)]
        J --> L[Layer 2A: Primary Strategy Exit: Underlying Index Spot Monitor]
        J --> M[Layer 2B: Emergency Catastrophic Floor: Broker OCO/Hard Stop]
        J --> N[Layer 3: Upfront Target 1 Limit Order: Gamma-Adjusted TP]
    end

    subgraph Persistence & Telemetry
        J --> O[Storage: market_context.options_sizing]
        J --> P[Discord Formatter: Greeks & Informational Theta Embed]
    end
```

---

## 3. Mathematical Specifications & Derivations

### 3.1 Expiry Policy: Retain Current Week Expiry
Based on the live Dhan API audit on NIFTY (`TASK-011`):
- **Current Week Expiry**: Peak market liquidity (21.2M vs 5.1M open interest, 4.16x higher), tightest bid-ask spread (0.05 tick), +66.7% higher Gamma (0.00175 vs 0.00105), lower capital per lot (₹105.75 vs ₹188.90).
- **Next Week Expiry**: Suffers a 75% to 95% liquidity cliff across strikes, doubles capital requirements, and widens execution slippage.
- **Decision**: Strictly retain **Current Week Expiry** and resolve theta decay through mathematical modeling and underlying-referenced stop triggers.

### 3.2 Strike Selection Policy: ATM-Biased ($\Delta \ge 0.45$, Closest to $0.50$)
To eliminate OTM decay traps while capturing maximum gamma acceleration:
1. Filter all contracts where absolute delta $|\Delta| \ge 0.45$.
2. Sort candidates by minimum distance to ATM:
   $$\text{rank\_key} = \left| |\Delta| - 0.50 \right|$$
3. Select candidate with minimum rank key. This guarantees selection of ATM / near-the-money contracts with intrinsic stability and high liquidity.

### 3.3 Second-Order Taylor Premium Projection (Delta + Gamma)
For a long option (Call or Put) with current entry reference price $P_0$ at underlying index spot $S_0$:
$$P(S) \approx P_0 + \Delta \cdot (S - S_0) + \frac{1}{2} \Gamma \cdot (S - S_0)^2$$

Where:
- For Calls: $\Delta > 0, \Gamma > 0$. When spot rises to target ($S > S_0$), $\Delta (S - S_0) > 0$ and $\frac{1}{2}\Gamma (S - S_0)^2 > 0$ provides positive acceleration.
- For Puts: $\Delta < 0, \Gamma > 0$. When spot falls to target ($S < S_0$), $\Delta (S - S_0) > 0$ and $\frac{1}{2}\Gamma (S - S_0)^2 > 0$ provides positive convexity.
- **Conservative Risk-Sizing Floor**: At spot stop loss ($S_{SL}$), the adverse spot move produces a negative first-order delta loss. While the positive gamma term reduces theoretical instantaneous loss, **risk sizing must use the linear delta loss as an upper-bound loss floor** to prevent dangerous over-sizing:
$$\text{RiskLossPts} = \max\left(P_0 - P(S_{SL}),\ |\Delta| \cdot |S_0 - S_{SL}|\right)$$

### 3.4 Time Decay ($\Theta$) & Volatility ($\mathcal{V}$) Scenario Adjustments
Let $\Delta t$ be the expected holding horizon in trading days (default intraday scalp horizon: $\Delta t = \frac{15\text{ min}}{375\text{ min}} \approx 0.04$ day):
$$P_{\text{projected}}(S, \Delta t, \Delta \sigma) = P_0 + \Delta \cdot (S - S_0) + \frac{1}{2}\Gamma \cdot (S - S_0)^2 + \Theta \cdot \Delta t + \mathcal{V} \cdot \Delta \sigma$$
- **Instantaneous Bracket Base**: For immediate limit entry and initial bracket construction, $\Delta t = 0$ and $\Delta \sigma = 0$ are used.
- **Target Feasibility Gate**: $P_{\text{projected}}(S_{T1}, \Delta t_{\text{expected}}, 0)$ must yield a net risk-to-reward $R \ge 1.0$ after adverse spread allowances.
- **Informational Telemetry**: Daily theta rate is published to Discord/console alerts as informational reference only; it is never used to artificially penalize lot sizing.

### 3.5 Model-Free Expected Move & Straddle Sanity Check
Leveraging the market-consensus formula from Obsidian (`Expected Move — Formula and Straddle Cheat Code.md`):
$$\text{Expected Move}_{1\sigma} \approx \text{ATM Straddle Price} \times 1.25$$
- When provider IV is missing or stale, $\text{ATM Straddle} \times 1.25$ provides a robust, model-free $1\sigma$ volatility substitute.
- If $|\text{IV-based } 1\sigma - \text{Straddle } 1\sigma|$ diverges significantly (>25%), emit an `IV_FEED_STALE` warning.

### 3.6 Strict Risk Sizing & Capital Hard-Borders
Let $C$ be the available balance returned from DhanHQ, and $r$ the configured risk fraction (`risk_per_trade_pct / 100.0`):
1. **Capital Validity**: If $C \le 0$ or balance fetch fails without verified fallback, set `suggested_lots = 0`, `affordable_lots = 0`, attach reason `NO_AVAILABLE_CAPITAL`, and abort execution.
2. **Sizing Math**:
   $$B = C \times r$$
   $$\text{PerLotLoss} = \text{RiskLossPts} \times \text{lot\_size}$$
   $$\text{suggested\_lots} = \begin{cases} \lfloor \frac{B}{\text{PerLotLoss}} \rfloor & \text{if } \text{PerLotLoss} > 0 \text{ and } C > 0 \\ 0 & \text{otherwise} \end{cases}$$
   $$\text{affordable\_lots} = \begin{cases} \lfloor \frac{C}{P_{\text{entry}} \times \text{lot\_size}} \rfloor & \text{if } P_{\text{entry}} > 0 \text{ and } C > 0 \\ 0 & \text{otherwise} \end{cases}$$
   $$\text{final\_lots} = \max(0, \min(\text{suggested\_lots}, \text{affordable\_lots}, \text{max\_lots\_cap}))$$
3. **Guaranteed Invariant**: $\text{final\_lots} \ge 0$ under all circumstances.

### 3.7 Monotonic Level Constraints & Tick Rounding
Every option price level must be rounded to the nearest valid exchange tick ($0.05$ for NSE options):
$$\text{round\_tick}(p) = \text{round}(p / 0.05) \times 0.05$$
For a long option contract, the executable levels must strictly satisfy:
$$0.05 \le \text{option\_sl} < \text{entry\_limit} < \text{option\_t1} < \text{option\_t2}$$
If this ordering fails (e.g. entry premium too low, or SL projected $\le 0$), the bracket is rejected with `INVALID_BRACKET_ORDERING`.

---

## 4. Execution Architecture: The Dual-Layer Stop-Loss Model

To reconcile the need for **upfront limit orders** with the critical insight that **hard premium stops suffer from false theta shakeouts**, ARES adopts a **Dual-Layer Execution Model**:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                   ARES DUAL-LAYER EXECUTION MODEL                       │
├────────────────────────────────────────────────────────────────────────┤
│ 1. Upfront Entry:                                                      │
│    Limit Order at P_limit = min(BestAsk, LTP + slippage_buffer)        │
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

### 4.1 Invariant: No Time-Stops (TASK-108)
Strictly maintain the [[TASK-108 Remove Automatic Time-Stop]] invariant. Do not implement automatic timer-based exits, elapsed candle count expirations, or forced break-even stops based solely on clock time.

### 4.2 Interaction with Exhaustion SL Audit (2026-10-13)
The recent 165-trade audit demonstrated that widening index stop distances (e.g., from 10/12 pts to 22-30 pts) mechanically rescues trades, but causes **78% to 100% of trades to fail the engine's `min_rr_ratio = 1.0` production gate** because $T_1$ remains at 18–24 pts.
- **Architectural Guard**: Stop loss geometry in `config_profiles.py` must never be widened in isolation without a matching expansion of $T_1$. For MANM-176, the option calculator strictly accepts whatever structural/per-type spot levels the engine provides and computes accurate option derivatives without overriding engine geometry.

### 4.3 Volatility-Adaptive Stop Loss Engine: 5-Minute ATR TS (Atr 5, HHV 10, Mult 2.5)
Per trader mandate, static SL multiples (1.5x–2.0x) are replaced with the **5-minute ATR Trailing Stop (ATR TS)**:
- **Inputs**:
  - Timeframe: `5m` candles
  - `atr_period`: `5`
  - `hhv_period`: `10` (Highest High Value / Lowest Low Value window = 10 bars = 50 minutes)
  - `multiplier`: `2.5`
- **Calculation**:
  $$\text{ATR}_5 = \frac{1}{5} \sum_{i=0}^4 \text{TrueRange}_{t-i}$$
  $$\text{HHV}_{10} = \max(\text{High}_t, \dots, \text{High}_{t-9})$$
  $$\text{LLV}_{10} = \min(\text{Low}_t, \dots, \text{Low}_{t-9})$$
  - **For Long (Bullish / CE)**:
    $$\text{ATR\_TS}_{\text{spot}} = \text{HHV}_{10} - \left( 2.5 \times \text{ATR}_5 \right)$$
    Trailed dynamically: $\text{SL}_t = \max(\text{SL}_{t-1}, \text{ATR\_TS}_{\text{spot}})$
  - **For Short (Bearish / PE)**:
    $$\text{ATR\_TS}_{\text{spot}} = \text{LLV}_{10} + \left( 2.5 \times \text{ATR}_5 \right)$$
    Trailed dynamically: $\text{SL}_t = \min(\text{SL}_{t-1}, \text{ATR\_TS}_{\text{spot}})$

- **Integration into Dual-Layer Model**:
  - **Layer 1 (Synthetic Invalidation)**: Uses $\text{ATR\_TS}_{\text{spot}}$ as the dynamic invalidation level. When Spot touches $\text{ATR\_TS}_{\text{spot}}$, ARES sends an immediate market order to exit.
  - **Layer 2 (Emergency Broker Floor)**: Translates the 5m ATR stop distance to option premium at entry:
    $$\text{Broker\_SL} = \max\left(0.05,\ P_{\text{entry}} - \left(|\text{Entry}_{\text{spot}} - \text{ATR\_TS}_{\text{spot}}| \times |\Delta|\right)\right)$$
  - **Advantage**: Automatically expands stop distance during high volatility (e.g. market open) and contracts during low volatility (lunch chop), preventing false shakeouts while anchoring strictly to the 10-period (50-minute) market structure.

---

## 5. Component Boundaries and File Assignments

### 5.1 `models.py`
Add explicit typing contracts:
```python
@dataclass
class OptionGreeks:
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float

@dataclass
class OptionOrderBracket:
    strike: int
    option_type: Literal["CE", "PE"]
    entry_limit: float
    synthetic_spot_sl: float     # Layer 1: NIFTY spot invalidation trigger
    catastrophic_opt_sl: float   # Layer 2: Emergency broker hard stop
    target_1: float              # Gamma-adjusted TP1 limit
    target_2: Optional[float]    # Gamma-adjusted TP2 limit
    lots: int
    greeks: OptionGreeks
    is_valid: bool
    rejection_reason: Optional[str] = None

# AresSignal extensions
@dataclass
class AresSignal:
    # Existing fields...
    bracket: Optional[OptionOrderBracket] = None
    options_sizing_status: str = "PENDING"  # "VALID", "NO_AVAILABLE_CAPITAL", "INVALID_DATA"
```

### 5.2 `fetchers/oi_fetcher.py`
- Update `fetch_chain()` to extract `delta`, `gamma`, `theta`, `vega`, and `iv` for all strikes across the full option chain table, rather than only populating them on the ATM row.
- Carry `ce_gamma`, `pe_gamma`, `ce_theta`, `pe_theta`, `ce_vega`, `pe_vega` into each row of `full_chain`.

### 5.3 `options_math.py`
Refactor existing linear functions into a pure, comprehensive calculation suite:
- `calculate_risk_amount(capital: float, risk_pct: float) -> float`: Clamps negative capital to 0.0.
- `find_optimal_strike(direction: str, full_chain: list) -> Tuple[Optional[int], Optional[str], Optional[float], Optional[OptionGreeks]]`: Filters $|\Delta| \ge 0.45$, sorts by $||\Delta| - 0.50|$ ascending.
- `calculate_greek_premium_translation(spot_delta: float, delta: float, gamma: float) -> float`: Implements the 2nd-order Taylor projection.
- `round_to_tick(price: float, tick_size: float = 0.05) -> float`: Exchange tick rounding.
- `validate_capital(capital: Optional[float]) -> Tuple[float, bool]`: Returns validated non-negative balance and validity flag.
- `calculate_lots(risk_amt: float, sl_points: float, lot_size: int) -> int`: Returns $\ge 0$ integer, returns 0 if inputs $\le 0$.
- `calculate_affordable_lots(capital: float, premium: float, lot_size: int) -> int`: Returns $\ge 0$ integer.
- `build_option_bracket(signal: AresSignal, full_chain: list, capital: float, config: TuningConfig) -> OptionOrderBracket`: Pure constructor for executable bracket.
- `process_options_calculation(...)`: Orchestrates strike selection, Greek calculation, bracket construction, and signal attachment without fabricating sentinel quotes.

### 5.4 `execution/order_manager.py`
- Implement `OrderManager` to handle Dhan limit order dispatch and contingent OCO bracket management.
- Pure order state machine supporting simulated execution and live Dhan API integration.

### 5.5 `storage.py` and `reports.py`
- Persist `market_context.options_sizing` containing `greeks` (`delta`, `gamma`, `theta`, `vega`), `entry_limit`, `option_sl`, `option_target`, `capital`, and `rejection_reason`.
- Enforce non-negative lot guard in `reports._option_rupees()` to prevent sign inversion on legacy dirty rows.

### 5.6 `alerts.py`
- Update `format_signal` Discord embed to present Greeks ($\Delta, \Gamma, \Theta, \mathcal{V}$), entry limit, bracket SL/TP, and validated lot allocation.

---

## 6. Definition of Done & Verification Checklist

- [ ] **Pure Greek Calculation Tests**: Verify 2nd-order Taylor expansion for Call and Put across positive and negative spot movements in `tests/unit/test_options_math.py`.
- [ ] **ATM Strike Selection Tests**: Verify selection selects $|\Delta| \ge 0.45$ closest to $0.50$ (reversing old OTM ascending bias).
- [ ] **Capital Boundary Regressions**: Test negative capital (`-39.19`), zero capital (`0.0`), and null capital, proving `suggested_lots == 0` and no negative lots are produced.
- [ ] **Quote Validity & Missing Data**: Verify that missing Greeks or LTP marks the bracket as invalid and does not inject fake quotes (LTP 100 / Delta 0.5).
- [ ] **Bracket Ordering Verification**: Test that inverted levels ($SL \ge \text{Entry}$) are caught and flagged with `INVALID_BRACKET_ORDERING`.
- [ ] **Tick Rounding**: Ensure all computed prices are exact multiples of 0.05.
- [ ] **Full Chain Greeks Verification**: Unit tests verifying `fetch_chain` returns full Greeks for all strikes.
- [ ] **Dual-Layer Stop Verification**: Unit test verifying synthetic spot SL and catastrophic emergency floor are computed cleanly.
- [ ] **Reporting Invariant**: Verify `reports._option_rupees()` never inverts P&L on negative lot inputs.
- [ ] **Pre-commit Doc Sync**: Sync ADR to repository docs and Obsidian vault.
