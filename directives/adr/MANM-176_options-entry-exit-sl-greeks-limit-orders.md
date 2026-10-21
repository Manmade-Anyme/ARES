# ADR-176: Comprehensive Options Entry, Exit, and SL Calculations Using Greeks, Index-Referenced Stops, and Upfront Limit Orders

- **Status**: PR #136 implemented; post-T1 ATR trailing added in PR #143 (2026-10-21), pending review/merge; live activation remains blocked
- **Date**: 2026-10-15
- **Task ID**: MANM-176
- **Author**: Software Architect Agent
- **Target Branch**: `codex/option-alert-visibility` (PR #143 follow-up)
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

The following diagram describes calculation and retained execution components. Main currently registers manual `SIGNAL_REFERENCE` lifecycles rather than submitting new entry orders; Section 7 defines that runtime boundary.

```mermaid
flowchart TD
    subgraph Market Ingestion & Contract Selection
        A[Engine Tick: AresSignal] --> B[Dhan Option Chain Snapshot]
        B --> C[Contract Selection: Retain Current Week Expiry]
        C --> D[Filter Abs Delta >= 0.45, Select Closest to ATM 0.50]
    end

    subgraph Greeks Math & Validation [options_math.py]
        D --> E{Quote and Greeks Available?}
        E -->|No| F[Unavailable data diagnosed; no fabricated prices or order]
        E -->|Yes| G[2nd-Order Taylor Expansion: Delta + Gamma]
        G --> H[Theta & Vega Horizon Scenario Adjustment]
        H --> I[Validate Monotonic Ordering: 0 < SL < Entry < T1 < T2]
        I -->|Ordering Invalid| F
        I -->|Valid| J[Display prices; capital determines nonnegative lots only]
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
- **Target Scenario Diagnostic**: The theta-adjusted T1 projection is retained as diagnostic data only. The `INSUFFICIENT_OPTION_RR` feasibility rejection has been removed; this scenario does not block pricing, lot suggestions or bracket validity.
- **Informational Telemetry**: Daily theta rate is published to Discord/console alerts as informational reference only; it is never used to artificially penalize lot sizing.

### 3.5 Model-Free Expected Move & Straddle Sanity Check
Leveraging the market-consensus formula from Obsidian (`Expected Move — Formula and Straddle Cheat Code.md`):
$$\text{Expected Move}_{1\sigma} \approx \text{ATM Straddle Price} \times 1.25$$
- The ATM straddle estimate is diagnostic context only. Missing provider IV still rejects an executable bracket; this estimate never fabricates a Greek or tradable premium.
- If $|\text{IV-based } 1\sigma - \text{Straddle } 1\sigma|$ diverges significantly (>25%), emit an `IV_FEED_STALE` warning.

### 3.6 Strict Risk Sizing & Capital Hard-Borders
Let $C$ be the available balance returned from DhanHQ, and $r$ the configured risk fraction (`risk_per_trade_pct / 100.0`):
1. **Capital Validity**: Capital affects lot allocation only. Calculate and display option entry, stop loss, T1 and T2 independently of account balance, when quote and Greeks inputs are available. Calculate prices and lot suggestions before execution-only ordering checks; those checks do not hide calculated values. If $C \le 0$ or balance fetch fails without verified fallback, set `suggested_lots = 0`, `affordable_lots = 0`, attach reason `NO_AVAILABLE_CAPITAL`, and block execution while retaining the calculated prices. A positive balance that cannot fund the risk or premium also retains prices with zero final lots and reason `INSUFFICIENT_CAPITAL_OR_RISK_BUDGET`. `is_valid` continues to require positive final lots for execution; these allocation-only rejections do not hide valid prices in alerts or persisted summaries.
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

### 4.3 Implemented Post-T1: 5-Minute ATR Trailing Stop
PR #143 implements the previously deferred roadmap. This documents branch behavior, not a production deployment or live-order activation.

- **Inputs**: NIFTY spot, completed `5m` candles, Wilder ATR period `5`, HHV/LLV window `10` bars, multiplier `2.5`. `atr_trailing.completed_atr_bars(candles)` aggregates exactly five contiguous, explicitly completed one-minute provider bars aligned to five-minute boundaries in the 09:15–15:30 IST session. Output timestamps denote bar ends. Forming bars never become complete merely through elapsed time; latest duplicate revisions win. Invalid/missing bars and session changes reset the ATR seed and channel window.
- **Math**: First true range is $H-L$. Thereafter:
  $$\mathrm{TR}_t=\max(H_t-L_t, |H_t-C_{t-1}|, |L_t-C_{t-1}|)$$
  The first five ranges seed ATR with their arithmetic mean; subsequent bars use:
  $$\mathrm{ATR}_t=(4\mathrm{ATR}_{t-1}+\mathrm{TR}_t)/5$$
  After ten contiguous five-minute bars, raw candidates are:
  $$\mathrm{CECandidate}_t=\mathrm{HHV}_{10}-2.5\mathrm{ATR}_t,\qquad
    \mathrm{PECandidate}_t=\mathrm{LLV}_{10}+2.5\mathrm{ATR}_t$$
- **Before T1**: Existing setup-specific spot stop and T1 trigger behavior remain. Initial option entry, SL and target premiums retain the Delta/Gamma projection. No detector geometry is changed.
- **At T1**: Move the active spot stop to spot entry. Spot analytics retain the 50% T1 booking model. Paper execution books whole lots: a one-lot position closes entirely; larger positions book `max(1, lots // 2)` lots and trail the remainder.
- **Close before ratchet**: For each newly completed five-minute bar after T1 activation, first compare its close with the previously active stop. CE exits only when close is **strictly below** that stop; PE exits only when close is **strictly above** it. Equality and wicks alone do not trigger this exit. If the runner survives, calculate the stop for subsequent bars:
  $$\mathrm{CEStop}_t=\max(\mathrm{Entry},\mathrm{PreviousStop},\mathrm{CECandidate}_t)$$
  $$\mathrm{PEStop}_t=\min(\mathrm{Entry},\mathrm{PreviousStop},\mathrm{PECandidate}_t)$$
  Stops are reported at two-decimal spot precision. During warmup or after a data gap, retain entry/the already locked stop until a channel candidate is available. Stops never loosen. A newly tightened stop cannot retrospectively exit the same candle.
- **Runner policy**: Fixed T2 remains a displayed reference; it no longer automatically exits post-T1 runners. Multi-session carry remains permitted. A new session resets ATR inputs while the previously locked trade stop remains active.
- **Persistence**: `market_context.atr_trailing` stores the processed-bar watermark, previous stop and ATR in `trade_analytics`, alongside the active state/stop. Startup restores the saved stop and watermark. Legacy T1 trades without a watermark anchor to the latest completed bar and resume prospectively, avoiding historical replay exits. ATR metadata and paper telemetry share the per-trade context lock to preserve each other's JSON keys.
- **Manual/reference signals**: Main tracks every emitted, persisted signal as `SIGNAL_REFERENCE`, independently of capital, suggested lots or bracket execution validity. It does not dispatch new paper or broker orders at signal entry. Spot T1/ATR updates therefore remain available at zero capital or with a rejected execution plan; tracking makes no fill assertion.
- **Paper execution**: An ATR close-through creates an exit intent for remaining quantity. The actual paper exit still requires a subsequently observed executable option bid. The initial emergency premium stop remains active; it is not replaced with a fabricated or dynamically projected premium stop. New live orders remain blocked.
- **Discord**: `ATR_SL_UPDATED` reports a tightened stop and previous stop; unchanged stops produce no update. `ATR_SL_HIT` reports the completed-close exit trigger. Optional option SL/exit values use persisted Greeks as labelled estimates, not confirmed fills.

Example for CE: entry 24,000; active stop 24,000; completed close 24,030 and candidate 24,008 tighten the next stop to 24,008. A later candidate 24,005 leaves it unchanged. A later close 24,007 triggers the runner exit at the observed close for spot analytics; it does not claim an option fill at 24,008 or at any model premium.

### 4.4 Trade Outcome Resolution & Multi-Class Scoring Contract (TASK-198 Alignment)
Per trader mandate and TASK-198 conventions, every trade resolution must preserve the verified outcome scoring taxonomy:
1. **Initial Stop Loss (`SL_HIT` / `STOPPED_OUT`)**:
   - *Trigger*: Spot hits initial structural stop loss before reaching $T_1$.
   - *Business Score*: `-1` (Full Loss on Trade).
   - *ML / Storage Label*: `0` in non-negative categorical label space (`trade_analytics.score`), with negative magnitude recorded in `pnl_points`.
2. **First Target Reached & Trailing Stop (`T1_HIT` / `ATR_SL_HIT`; legacy `STOPPED_OUT_AT_BE`)**:
   - *Trigger*: Spot reaches setup T1; spot analytics model 50% booked there and the runner exits on a completed five-minute close through its active stop. `ATR_SL_HIT` records that close as exit price; signed spot P&L is the mean of entry-to-T1 and entry-to-close movement.
   - *Business Score*: `+1` (Banked partial profit, protected runner).
   - *ML / Storage Label*: `1` in `trade_analytics.score` and `ml_collection.trade_score`.
3. **Historical Second Target Reached (`T2_HIT`)**:
   - *Historical trigger*: Spot reached $T_2$ for a full-target exit. Existing records retain score 2; the current post-T1 ATR runner does not automatically exit at fixed T2.
   - *Business Score*: `+2` (Double Win / Multi-target winner).
   - *ML / Storage Label*: `2` in `trade_analytics.score` and `ml_collection.trade_score`.

This scoring contract preserves 100% telemetry continuity across `PositionManager`, `storage.py`, `trade_analytics`, `ml_collection`, and daily performance audit reconciliations.

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
    security_id: str             # Resolved Dhan instrument token (e.g. "40710")
    expiry_date: str             # Current-week expiry string (e.g. "2026-10-15")
    strike: int
    option_type: Literal["CE", "PE"]
    entry_limit: float           # Executable limit price (0.05 tick rounded)
    synthetic_spot_sl: float     # Layer 1: NIFTY spot invalidation trigger
    catastrophic_opt_sl: float   # Layer 2: Emergency broker hard stop
    target_1: float              # Gamma-adjusted TP1 limit
    target_2: Optional[float]    # Gamma-adjusted TP2 limit
    lots: int                    # Final planned lots (capped by max_lots_cap & affordability; fills tracked separately)
    greeks: OptionGreeks
    is_valid: bool
    rejection_reason: Optional[str] = None

# AresSignal extensions
@dataclass
class AresSignal:
    # Existing fields...
    bracket: Optional[OptionOrderBracket] = None
    options_sizing_status: str = "PENDING"  # "VALID", "NO_AVAILABLE_CAPITAL", "INSUFFICIENT_CAPITAL_OR_RISK_BUDGET", "INVALID_DATA"
```

### 5.2 `config_profiles.py`
Add configuration fields to `TuningConfig` for sizing and execution guards:
- `options_max_lots_cap: int = 10`: Maximum allowed lot allocation per trade.
- `options_slippage_buffer_pts: float = 0.50`: Tolerable slippage buffer when computing entry limit from ask.

### 5.3 `fetchers/oi_fetcher.py`
- Update `fetch_chain()` to extract `security_id` (instrument token), `expiry_date`, `delta`, `gamma`, `theta`, `vega`, and `iv` for all strikes across the full option chain table, rather than only populating them on the ATM row.
- Carry `security_id`, `expiry_date`, `ce_gamma`, `pe_gamma`, `ce_theta`, `pe_theta`, `ce_vega`, `pe_vega` into each row of `full_chain`.

### 5.4 `options_math.py`
Refactor existing linear functions into a pure, comprehensive calculation suite:
- `calculate_risk_amount(capital: float, risk_pct: float) -> float`: Clamps negative capital to 0.0.
- `find_optimal_strike(direction: str, full_chain: list) -> Tuple[Optional[int], Optional[str], Optional[float], Optional[OptionGreeks], Optional[str]]`: Filters $|\Delta| \ge 0.45$, sorts by $||\Delta| - 0.50|$ ascending; returns strike, option_type, ltp, greeks, and security_id.
- `calculate_greek_premium_translation(spot_delta: float, delta: float, gamma: float) -> float`: Implements the unified 2nd-order Taylor projection.
- `round_to_tick(price: float, tick_size: float = 0.05) -> float`: Exchange tick rounding.
- `validate_capital(capital: Optional[float]) -> Tuple[float, bool]`: Returns validated non-negative balance and validity flag.
- `calculate_lots(risk_amt: float, sl_points: float, lot_size: int) -> int`: Returns $\ge 0$ integer, returns 0 if inputs $\le 0$.
- `calculate_affordable_lots(capital: float, premium: float, lot_size: int) -> int`: Returns $\ge 0$ integer.
- `build_option_bracket(signal: AresSignal, full_chain: list, capital: Optional[float], config: TuningConfig) -> OptionOrderBracket`: Pure constructor that prices independently of capital and applies `config.options_max_lots_cap` and `config.options_slippage_buffer_pts`. Zero-lot brackets retain valid prices but cannot execute.
- `process_options_calculation(...)`: Orchestrates strike selection, Greek calculation, bracket construction, and signal attachment without fabricating sentinel quotes.

### 5.5 `execution/order_manager.py` & `main.py` Wiring
- Implement `OrderManager` to handle Dhan limit order dispatch and contingent OCO bracket management.
- Paper order state machine with observed quote fills and completed-candle ATR runner exits; new live dispatch remains blocked.
- **`main.py` wiring**: Main registers every emitted, persisted signal through `PositionManager.add_trade()` with `tracking_mode=SIGNAL_REFERENCE`; it does not dispatch a new paper/broker entry order. Manual spot tracking is independent of lot allocation and executable-bracket checks. Existing order-manager APIs and paper monitoring remain supported/tested for execution records. Completed provider history reaches both `PositionManager.update_atr_trailing(candles)` and the dispatcher’s `on_candles(candles)`. `atr_trailing.completed_atr_bars(candles)` is the shared pure ATR calculation; each consumer persists its own trade activation/watermark.

### 5.6 `storage.py` and `reports.py`
- Persist `market_context.options_sizing` containing `greeks` (`delta`, `gamma`, `theta`, `vega`), `security_id`, `entry_limit`, `option_sl`, `option_target`, `capital`, and `rejection_reason`.
- Enforce non-negative lot guard in `reports._option_rupees()` to prevent sign inversion on legacy dirty rows.

### 5.7 `alerts.py` (Discord Alert Specification)

Overhaul signal dispatch and trade lifecycle alerts to provide full transparency on Greeks, bracket limit orders, and option performance:

#### 1. Signal Detection Alert (`format_signal` & `send_discord`)
Display bracket levels and optional diagnostics in the Discord Embed:
- **Discord verbosity**: `TuningConfig.discord_show_option_details` defaults to `False` in both day profiles and hides `⚡ Order Type`, `📊 Option Greeks` and `💰 Risk Budget`. Set it to `True` to restore those three fields. Console diagnostics retain all details. This presentation flag does not change pricing, sizing or execution.
- **Order Execution Type**:
  - `⚡ Order Type`: `LIMIT (Upfront Bracket)`
  - `🎯 Entry Limit`: `₹ {bracket.entry_limit:.2f}` (Calculated limit plan; separately display `LTP: ₹ {signal.option_premium:.2f}`)
- **Greeks Breakdown Field**:
  - `📊 Option Greeks`: `Δ {delta:+.3f} | Γ {gamma:.4f} | Θ {theta:.1f}/day | IV {iv:.1f}%`
- **Dual Target Breakdown**:
  - `🎯 Target 1`: `₹ {target_1:.2f}` (50% position book | Spot: `{t1_spot:.1f}`)
  - `🎯 Target 2`: `₹ {target_2:.2f}` (Reference only; runner trails ATR | Spot: `{t2_spot:.1f}`)
- **Option Stop Loss Field**:
  - Discord `🛑 Stop Loss`: `₹ {option_sl:.2f}` in both verbosity modes; the existing spot `🛑 SL` field already shows structural invalidation. Console diagnostics retain both stop layers.
- **Capital & Sizing Breakdown**:
  - `📐 Sizing`: `{bracket.lots} Lots ({bracket.lots * lot_size} qty)` (Final planned lots, capped by max cap; not a fill assertion)
  - `💰 Risk Budget`: `₹{risk_amt:.0f} ({risk_pct:.1f}% of ₹{capital:.0f} | Uncapped Suggestion: {bracket.uncapped_suggested_lots} lots)`

#### 2. Trade Lifecycle Update Alert (`send_trade_update`)
Enrich spot lifecycle alerts with explicitly labelled option premium estimates. These messages do not establish broker fills or realized option P&L. The 50% booking example is a model; executable allocations use whole lots (a one-lot trade exits at T1). Spot break-even does not imply option break-even:
- **`T1_HIT` Update**:
  - `⚡ Action`: `Target 1 Hit (spot); partial-booking estimate`
  - `💰 Estimated Option Exit`: `₹ {t1_option_price:.2f} (+₹ {pnl_per_lot:.2f} / lot)`
  - `🛡️ SL Status`: `Trailed to Break-Even (Spot {entry_price:.2f} / Option ₹{entry_premium:.2f})`
- **`ATR_SL_UPDATED` Update**:
  - `⚡ Action`: `ATR trailing stop tightened.`
  - Show current and previous spot SL, the 5-minute ATR method and an estimated option SL when stored pricing data supports it. No update is sent when the stop does not tighten.
- **`ATR_SL_HIT` Update**:
  - `⚡ Action`: `5-minute close crossed ATR Stop Loss. Runner exit triggered.`
  - Show completed spot close, active/previous SL and any calculated option exit explicitly labelled as a model estimate. This does not assert a paper or broker fill.
- **Legacy `STOPPED_OUT_AT_BE` Update**:
  - `⚡ Action`: `Runner reached spot break-even; option exit estimate`
  - `💰 Estimated Trade PnL`: `₹ {net_trade_pnl:.2f} (50% T1 booking model; not guaranteed)`
- **`SL_HIT` Update**:
  - `⚡ Action`: `Stop Loss Triggered`
  - `🛑 Exit Price`: `₹ {exit_premium:.2f} (-₹ {loss_per_lot:.2f} / lot)`
- **Legacy `T2_HIT` Update**:
  - `⚡ Action`: `Target 2 Hit (spot); runner exit estimate`
  - `💰 Estimated Option Exit`: `₹ {t2_option_price:.2f} (+₹ {runner_pnl:.2f} / lot)`

---

## 6. Definition of Done & Verification Checklist

- [x] **Pure Greek Calculation Tests**: Verify 2nd-order Taylor expansion for Call and Put across positive and negative spot movements in `tests/unit/test_options_math.py`.
- [x] **ATM Strike Selection Tests**: Verify selection selects $|\Delta| \ge 0.45$ closest to $0.50$ (reversing old OTM ascending bias).
- [x] **Capital Boundary Regressions**: Test negative capital (`-39.19`), zero capital (`0.0`), and null capital, proving `suggested_lots == 0` and no negative lots are produced.
- [x] **Quote Validity & Missing Data**: Verify that missing Greeks or LTP marks the bracket as invalid and does not inject fake quotes (LTP 100 / Delta 0.5).
- [x] **Bracket Ordering Verification**: Test that inverted levels ($SL \ge \text{Entry}$) are caught and flagged with `INVALID_BRACKET_ORDERING`.
- [x] **Tick Rounding**: Ensure all computed prices are exact multiples of 0.05.
- [x] **Full Chain Greeks Verification**: Unit tests verifying `fetch_chain` returns full Greeks for all strikes.
- [x] **Dual-Layer Stop Verification**: Unit test verifying synthetic spot SL and catastrophic emergency floor are computed cleanly.
- [x] **Reporting Invariant**: Verify `reports._option_rupees()` never inverts P&L on negative lot inputs.
- [x] **Pre-commit Doc Sync**: Sync ADR to repository docs in PR #136. Obsidian production documentation sync occurs locally after the user merges, per the Global Development Pipeline.


## 7. Implementation Contract (PR #136 and PR #143 follow-up)

This section specifies the implemented scope and qualifies illustrative execution claims above. PR #143 adds Section 4.3 ATR behavior and removes the option R:R feasibility rejection. No deployment or live account activation is included.

### Calculation and persistence
- `OIFetcher.fetch_chain()` preserves side-specific security IDs, expiry, underlying spot reference, bid/ask, IV and all four Greeks across the chain. Missing and nonfinite numeric data remain missing.
- `build_option_bracket()` selects a complete current-expiry contract nearest absolute delta 0.50, validates direction and positive quotes, and projects the supplied spot SL/T1/T2 with signed Delta and Gamma. It does not change detector geometry.
- Entry limits are rounded down when nearest-tick rounding would exceed the ask/slippage bound. Prices must remain strictly ordered after tick rounding. Risk sizing includes the worst permitted entry and adverse spread, with a linear-Delta loss floor. The 15-minute theta scenario is diagnostic only; there is no option R:R rejection and it does not change allocation. Vega is retained for scenario analysis, with zero IV change for the initial bracket.
- Capital fetch failure has no unverified default-capital fallback. Invalid capital, missing quotes/Greeks, invalid order geometry or insufficient allocation produce a rejection and zero executable lots.
- `market_context.options_sizing.final_lots` is the capped planned allocation. `uncapped_suggested_lots` retains the risk-only suggestion. Legacy `suggested_lots` represents the final allocation. Contract, Greeks, capital, lot size and levels persist in trade context; rejected signals retain the rejection reason in their signal audit reasons.
- Expected-move diagnostics compare IV and ATM straddle estimates when both are available. A missing IV is not substituted into an executable bracket.

### Execution boundaries
The following paper-order contracts describe the retained order-manager APIs/monitoring. Main’s new-signal path performs manual reference tracking and submits no new entry orders.

- `OrderManager` defaults to paper mode. Dispatch records a pending limit plan; submission alone does not constitute a fill. Paper entries require an observed executable ask at or below the limit, and paper exits require an observed bid.
- Paper invalidation cancels pending entries at the structural stop, target or expiry. Filled paper positions use the spot stop and emergency premium floor. T1 closes whole lots: one lot closes entirely, otherwise the first allocation is `max(1, lots // 2)`. Remaining lots use completed-five-minute-close ATR trailing after moving the spot stop to entry. T2 remains a reference, not an automatic runner exit. A pending exit without a bid remains pending; the initial emergency premium floor remains active.
- `DhanSuperOrderAdapter` exposes an independently tested low-level LIMIT super-order payload and order-book reads for an injected SDK. `OrderManager` rejects every new live dispatch with `LIVE_RUNTIME_UNSUPPORTED` before calling the broker. `allow_live=True` plus a durable single-writer journal permits recovery inspection only; it does not enable new orders. Reconciliation requires cumulative fill and protective-leg evidence.
- The user explicitly chose to keep live activation blocked on 2026-10-16. A subsequent reviewed design must establish synthetic exit/cancellation race handling, partial fills, emergency flattening, complete exit reconciliation, DAY protection versus overnight carry, expiry settlement and whitelisted static egress. Recovered live records remain inspection-only; they are not automatically managed by the paper runtime.
- Stop orders and synthetic stop triggers cannot guarantee a maximum realized loss or fill during gaps, illiquidity or infrastructure failure.

### Runtime integration and recovery
- Main registers persisted, emitted signals as manual `SIGNAL_REFERENCE` spot trades without dispatching new entry orders. Capital, lot allocation and executable-bracket checks do not prevent that registration or its T1/ATR lifecycle. Existing paper-order APIs still require protected observed fills for execution-backed registration; a submission acknowledgment is not a fill. Existing pending execution records remain deduplicated and monitored.
- Paper quote processing uses the matched contract row's finite, positive `underlying_spot` for the fill reference, persisted `spot_at_fill`, analytics entry and post-T1 break-even level. Missing, nonfinite, boolean or nonpositive quote spot prevents quote execution; independent spot ticks can still invalidate pending entries or trigger synthetic exits. A delayed fill retains its actual fill timestamp. Candle wicks from before that fill are excluded from the new trade's first exit evaluation.
- Every protected observed fill is registered, including a record already `CLOSED` or `EXIT_PENDING` before registration. A fill and emergency exit on the same quote persist their quantities, entry price, exit reason and realized paper P&L in trade execution context. Registration retries retain the original fill spot and timestamp.
- Paper execution transitions update trade analytics and bind the original canonical signal snapshot to the trade. `PositionManager.sync_execution()` returns `False` until that snapshot exists and its trade link is confirmed, then `True`. Filled terminal records remain available for persistence retries after main writes the snapshot; they are excluded from pending-entry deduplication. Failures remain retryable without preventing other orders or spot processing.
- On startup, `PositionManager` uses its existing analytics reconciliation read to restore `market_context` for nonterminal active trades by trade ID. Both entry RPCs persist that context in `trade_analytics`; a schema migration is unnecessary. Persisted analytics keys override matching active-row keys, while unrelated active-row keys survive. Legacy trades without usable analytics context retain their existing context, and analytics read failures remain best-effort so active-trade monitoring can still initialize. Terminal analytics reconciliation continues to exclude already resolved trades.
- Restored `options_sizing`, `execution` and `atr_trailing` context remain available to carried-trade lifecycle reporting. Spot runner stop/state/watermark resume from persisted analytics. Execution context describes the last persisted paper state; application startup does not reconstruct pending orders or resume paper simulation.
- Paper T1 target limits may fill from observed bid quotes before the corresponding spot target. Execution telemetry and historical spot outcome scoring remain separate models; neither is live broker P&L.
- Pending paper orders exist only for the current runtime session. Restarted spot analytics continue their established carry behavior; the application does not reconstruct a broker portfolio or claim live protection. Optional order journals support state-machine recovery tests, not production live activation.

### Reporting interpretation
- Signal alerts show available calculated limits separately from LTP and capital-based suggested quantity even when execution is rejected. Missing provider data is explicitly unavailable. Discord hides optional Greeks/order/risk diagnostics by default and shows one premium stop; console diagnostics retain details. Neither display claims submission or execution.
- Lifecycle option prices and the 50% booking P&L example are estimates, not broker fills. In particular, a spot break-even exit can still lose option premium after theta, IV changes, spread and fees.
- Aggregate option P&L retains the existing linear-Delta approximation; one-lot bracket P&L uses the stored plan. Both are labelled estimates and use persisted lot size where available. Neither is a realized-fill accounting ledger.
- Spot outcome labels remain `0` for initial loss, `1` for post-T1 trailing resolution (`ATR_SL_HIT`) and `2` for historical T2 records. These labels do not assert realized option profit.

### Verification and delivery
Focused contracts live in `tests/unit/test_options_math.py`, `test_order_manager.py`, `test_options_alert_reporting.py`, `test_oi_fetcher.py`, `test_manm176_persistence.py`, `test_manm176_dispatch.py` and `test_manm176_review_persistence.py`. Review regressions cover synchronized quote spot, invalid quote spot, same-quote emergency exits, registration/persistence retries, canonical ML linkage in both schema modes and carried-trade context restoration after restart. Final QA results are recorded in `reports/qa/MANM-176_implementation.md`; implementation documentation does not claim deployment validation. ATR public contracts additionally cover `tests/unit/test_atr_trailing.py` and the position-manager, dispatcher, paper-order and alert regression suites. Repository documentation and CHANGELOG stay on the implementation PR; Obsidian production sync follows manual merge.
