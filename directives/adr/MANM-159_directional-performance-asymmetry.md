# ADR-159: Resolve Directional Performance Asymmetry via Regime-Aware Gating, Setup Pruning, and Asymmetric Geometry

- **Status**: Proposed
- **Date**: 2026-09-12
- **Task ID**: MANM-159
- **Author**: Software Architect Agent (`d2d4e328-096d-4658-8d90-44aa7b51ed05`)
- **Applies to**: `engine.py`, `config_profiles.py`, `detectors/continuation.py`, `position_manager.py`

---

## 1. Executive Summary & Diagnostic Root Cause Analysis

### Problem Statement
An audit of the 233 historical trades in `trade_analytics` (spanning 2026-07-20 to 2026-09-11) revealed severe directional performance asymmetry between bearish and bullish setups:
- **Bearish Trades**: 127 trades, **+446.90 points**, 18.9% win rate to T2 (33.1% win rate to T1/score >= 1), mean +3.52 pts/trade, Profit Factor 1.44, Payoff Ratio 3.83.
- **Bullish Trades**: 106 trades, **-176.05 points**, 13.2% win rate to T2 (31.1% win rate to T1/score >= 1), mean -1.66 pts/trade, Profit Factor 0.79, Payoff Ratio 3.33.

Despite comparable win rates to first target (~31% vs 33%), bullish trades generate substantial aggregate losses (-176.05 pts), while bearish trades account for 100% of net system profits (+446.90 pts).

---

### Empirical Investigation & Statistical Diagnostics

Rigorous statistical diagnostics were conducted across the complete 233-trade population in Supabase `trade_analytics` and `active_trades`:

#### 1. Outlier Sensitivity & Bootstrap Confidence Interval Analysis
| Metric | Bearish (N=127) | Bullish (N=106) | Interpretation |
|---|---|---|---|
| **Total PnL** | +446.90 pts | -176.05 pts | 622.95 pt net disparity |
| **Mean Return** | +3.52 pts | -1.66 pts | Skewed by right-tail winners |
| **Median Return** | **-10.00 pts** | **-10.00 pts** | **Identical median loss!** |
| **5% Trimmed Mean** | **-0.74 pts** | -5.32 pts | Bearish flips negative when trimming 5% tails |
| **10% Trimmed Mean** | **-3.65 pts** | -6.25 pts | Both directions are negative on trimmed basis |
| **5% Winsorized Mean** | +2.56 pts | -3.69 pts | Mitigates single extreme outlier |
| **10% Winsorized Mean** | +1.46 pts | -4.55 pts | Stable positive vs negative drift |
| **Bootstrap 95% CI (Mean)** | `[-1.90, +9.63] pts` | `[-5.77, +3.21] pts` | Both CIs span across zero |
| **Bootstrap 95% CI (Total)** | `[-255.2, +1221.0] pts` | `[-613.0, +317.0] pts` | Substantial variance in both cohorts |
| **Top 1 Winner** | +201.45 pts | +116.65 pts | Top 1 Bearish winner is 45.1% of all net profit |
| **Top 5 Winners** | +521.45 pts | +374.75 pts | Top 5 Bearish = [201.45, 80.0, 80.0, 80.0, 80.0] |
| **Excluding Top 5 Winners** | **-74.55 pts** | -590.80 pts | **Bearish edge vanishes without top 5 runners** |

**Finding**: Bearish profitability is **not** a consistent across-the-board edge (the median trade is -10.00 pts in both directions). Rather, Bearish performance is propelled by **extreme positive right-tail skew** from large multi-leg runners (+201.45 and +80.0 pts) during a secular downward trend.

---

#### 2. Macro Temporal Regime Skew (The "Falling Knife" Phenomenon)
Analysis of the underlying index (NIFTY 50 spot) across the test horizon (2026-07-20 to 2026-09-11):
- First trade entry spot (2026-07-20): **24,181.40**
- Last trade entry spot (2026-09-11): **23,348.25**
- **Net Market Movement**: **-833.15 points (severe macro downtrend)**.

Performance breakdown by month:
| Period | Macro Index Context | Bearish PnL (Trades) | Bullish PnL (Trades) |
|---|---|---|---|
| **2026-07** | Consolidating / Bullish Drift | -53.00 pts (21) | **+190.55 pts (36)** |
| **2026-08** | Severe Macro Selloff (-700 pts) | **+273.90 pts (73)** | **-336.60 pts (54)** |
| **2026-09** | Continuation Selloff (-250 pts) | **+226.00 pts (33)** | **-30.00 pts (16)** |

**Finding**: Bullish setups actually **outperformed** Bearish setups in July (+190.55 pts vs -53.00 pts) when the market was neutral/bullish. The entire directional asymmetry occurred during August and September 2026 when Nifty fell ~1,000 points. Counter-trend long trades taken during an un-gated macro downtrend suffered severe drawdown.

---

#### 3. Setup-Specific Disparity & Toxic Setup Identification
Cross-tabulation of setup types by direction reveals where the loss occurs:
| Setup Type | Bearish Trades | Bearish PnL | Bullish Trades | Bullish PnL | Asymmetry / Diagnosis |
|---|---|---|---|---|---|
| `EXHAUSTION_REVERSAL` | 52 | **+45.75 pts** (+0.88/t) | 54 | **+47.15 pts** (+0.87/t) | **Zero asymmetry!** Identical positive expectancy (+0.88 vs +0.87). |
| `TREND_CONTINUATION` | 60 | **+437.70 pts** (+7.30/t) | 30 | **-65.00 pts** (-2.17/t) | **Disparity: +502.70 pts.** Bearish had 2x trades and 5x T2 hits (10 vs 2). Bullish had 56.7% stopped at BE. |
| `OI_WALL_REJECTION` | 7 | -40.00 pts (-5.71/t) | 13 | **-103.20 pts** (-7.94/t) | **Disparity: -63.20 pts.** 7.7% win rate. Buying put wall bounces in a crashing market fails. |
| `FAILED_BREAKOUT` | 8 | +3.45 pts (+0.43/t) | 9 | **-55.00 pts** (-6.11/t) | **Disparity: -58.45 pts.** 0 T2 hits (6 SL, 3 BE). Bullish breakout fades collapse. |

**Critical Insight**:
- `EXHAUSTION_REVERSAL` is completely symmetric and robust across both regimes.
- Bullish `OI_WALL_REJECTION` (-103.20 pts) and Bullish `FAILED_BREAKOUT` (-55.00 pts) represent **-158.20 points of drag** (89.9% of all bullish losses!).
- Bullish `TREND_CONTINUATION` suffered from target/geometry mismatch: rallies in Nifty were truncated and choppy, resulting in 56.7% BE stop-outs before reaching the distant 80-point T2.

---

#### 4. Options Pricing & Greeks Parity Analysis
Detailed inspection of option contract telemetry logged in `trade_analytics.market_context` and `ares_signals`:
- **Delta at Entry**: PE Mean = `-0.490` (Std: 0.033) vs CE Mean = `+0.491` (Std: 0.049).
- **Option Premium**: PE Mean = `₹89.97` vs CE Mean = `₹93.55`.
- **PCR at Entry**: PE Mean = `1.178` vs CE Mean = `1.177`.

**Finding**: Option selection is operating symmetrically at true ATM ($\Delta \approx 0.49$). The asymmetry does **not** stem from option pricing, delta distortion, or IV mispricing.

---

#### 5. Target/Stop Geometry Mismatch
- Current geometry in `_PER_TYPE_LEVELS_DEFAULT` is directionally symmetric:
  - `TREND_CONTINUATION`: SL = 25.0, T1 = 25.0, T2 = 80.0 (fallback).
- In Nifty index microstructure, market drops are rapid, high-momentum waterfalls that easily penetrate 80+ point extensions to hit T2. Upward movements (particularly in a macro corrective regime) are slow, choppy, and mean-reverting; they stall around 25-45 points, trigger break-even trailing, and reverse back to entry (producing 31.1% overall BE exits and 56.7% BE exits in continuation).

---

## 2. Decision & Remediation Architecture

To systematically remediate directional performance asymmetry without degrading the profitable bearish edge, four architectural changes are approved:

```
                                  [ Incoming Market Tick ]
                                             │
                                             ▼
                             [ Higher-Timeframe Regime Gate ]
                             (Macro Trend: VWAP + PDH/PDL)
                                    /                 \
                     Bullish Regime                     Bearish Regime
                    (close > VWAP & PDL)              (close < VWAP & PDH)
                            │                                   │
              ┌─────────────┴─────────────┐       ┌─────────────┴─────────────┐
              │ Allow All Bullish Setups  │       │ Allow All Bearish Setups  │
              │ Allow Exhaustion Reversal │       │ Allow Exhaustion Reversal │
              │ Allow Trend Continuation  │       │ Block Bullish Fades (OI/FB)│
              └───────────────────────────┘       │ Require Strict TC Volume  │
                                                  └───────────────────────────┘
                                                                │
                                                                ▼
                                                 [ Asymmetric Level Engine ]
                                                Bullish: T1=20, T2=50 (Quick)
                                                Bearish: T1=25, T2=80 (Runner)
```

### 1. Macro Regime-Aware Gating (Restoring Filter E Protection)
- **Policy**: When the session regime is confirmed **Bearish** (`close < vwap AND close < pdh`):
  - **Hard Gate**: Suppress Bullish `OI_WALL_REJECTION` and Bullish `FAILED_BREAKOUT`. In a macro selloff, attempting to catch falling knives at put walls or fading upside attempts has a 7.7% win rate and accounts for -158.20 pts in losses.
  - **Permit**: Bullish `EXHAUSTION_REVERSAL` remains active globally (+47.15 pts baseline; robust across regimes).
  - **Filter Simulation Result**: Eliminating bullish fades increases portfolio net PnL from **+270.85 to +429.05 points (+58.4% gain)**, and reduces bullish drag from -176.05 to -17.85 pts.

### 2. Strengthen Bullish Continuation Confirmation
- In `detectors/continuation.py`:
  - Increase the required resumption volume ratio for **Bullish** continuations from `>= 1.2x` to `>= 1.4x` rolling average volume.
  - Require the candle close to be above the 20-period moving average on the entry candle.
  - This eliminates low-volume, corrective "dead-cat" bounces in down-trending sessions.

### 3. Directional Asymmetric Risk/Reward Geometry
- Update `SetupLevels` and `config_profiles.py` to support direction-specific targets:
  - **Bearish `TREND_CONTINUATION`**: Maintain runner geometry (`SL=25.0, T1=25.0, T2_fallback=80.0`).
  - **Bullish `TREND_CONTINUATION`**: Calibrate to realistic index rally geometry (`SL=20.0, T1=20.0, T2_fallback=50.0`).
    - Lowering T2 from 80 to 50 points converts stalled BE trades into realized T2 wins before the market pulls back.
  - **Bullish `EXHAUSTION_REVERSAL`**: Maintain proven levels (`SL=10.0, T1=18.0, T2_fallback=40.0`).

### 4. Break-Even Buffer Protection
- In `position_manager.py`:
  - When a bullish trade reaches T1 and trails to Break-Even, apply a small protective profit lock:
    - For Bullish: `trade["stop_loss"] = trade["entry_price"] + 2.0` (locks in minimal positive scratch trade to cover transaction costs and slippage rather than exiting at exact 0.0 or taking intrabar adverse wicks).

---

## 3. Interface & Contract Specifications

### 1. `config_profiles.py` Data Model
Add `DirectionalSetupLevels` or directional overrides to `TuningConfig`:

```python
@dataclass(frozen=True)
class SetupLevels:
    stop_pts: float
    target_1_pts: float
    target_2_fallback_pts: float

@dataclass
class TuningConfig:
    # ... existing fields ...
    
    # Asymmetric geometry overrides: key format "{setup_type}_{direction}"
    # If not present, falls back to per_type_levels[setup_type]
    directional_levels: Dict[str, SetupLevels] = field(default_factory=lambda: {
        "TREND_CONTINUATION_BULLISH": SetupLevels(stop_pts=20.0, target_1_pts=20.0, target_2_fallback_pts=50.0),
        "TREND_CONTINUATION_BEARISH": SetupLevels(stop_pts=25.0, target_1_pts=25.0, target_2_fallback_pts=80.0),
        "EXHAUSTION_REVERSAL_BULLISH": SetupLevels(stop_pts=10.0, target_1_pts=18.0, target_2_fallback_pts=40.0),
        "EXHAUSTION_REVERSAL_BEARISH": SetupLevels(stop_pts=10.0, target_1_pts=18.0, target_2_fallback_pts=40.0),
        "OI_WALL_REJECTION_BULLISH":   SetupLevels(stop_pts=12.0, target_1_pts=20.0, target_2_fallback_pts=35.0),
        "OI_WALL_REJECTION_BEARISH":   SetupLevels(stop_pts=16.0, target_1_pts=25.0, target_2_fallback_pts=40.0),
        "FAILED_BREAKOUT_BULLISH":     SetupLevels(stop_pts=10.0, target_1_pts=15.0, target_2_fallback_pts=40.0),
        "FAILED_BREAKOUT_BEARISH":     SetupLevels(stop_pts=12.0, target_1_pts=20.0, target_2_fallback_pts=55.0),
    })

    # Regime gating flags
    gate_counter_trend_fades: bool = True
    bullish_continuation_volume_ratio: float = 1.4
```

### 2. `engine.py` Level Resolution Contract
Update `apply_per_type_levels`:

```python
def apply_per_type_levels(signal: AresSignal, settings, levels=None) -> None:
    """
    Set directional SL/T1/T2 for a signal in place.
    Checks settings.directional_levels first (e.g. 'TREND_CONTINUATION_BULLISH'),
    falling back to settings.per_type_levels[setup_type].
    """
    key = f"{signal.setup_type.value}_{signal.direction.value}"
    lv = getattr(settings, "directional_levels", {}).get(key)
    if lv is None:
        lv = settings.per_type_levels.get(signal.setup_type.value)
    if lv is None:
        return

    entry = signal.trigger_price
    sign = 1 if signal.direction == Direction.BULLISH else -1
    signal.stop_loss = entry - sign * lv.stop_pts
    signal.target_1 = entry + sign * lv.target_1_pts
    signal.target_2 = _resolve_target_2(entry, sign, signal.target_1, lv, levels)
```

### 3. `engine.py` Macro Regime Filter Hook
In `AresEngine.tick()`, evaluate regime against candidate signals:

```python
# Counter-Trend Fade Gating in Bearish Macro Regime
if settings.gate_counter_trend_fades and pdh is not None and pdl is not None:
    is_downtrend = candle.close < candle.vwap and candle.close < pdh
    if is_downtrend and signal and signal.direction == Direction.BULLISH:
        if signal.setup_type in (SetupType.OI_WALL_REJECTION, SetupType.FAILED_BREAKOUT):
            print(f"[-] AresEngine: Suppressing Bullish {signal.setup_type.value} signal — counter-trend fade in confirmed macro downtrend.")
            signal = None
```

---

## 4. Implementation Task Breakdown for Code Generator Agent

Assign implementation of ticket **MANM-159** to the **Code Generator Agent** across the following files:

### Task Breakdown

1. **`config_profiles.py`**:
   - Add `directional_levels` dictionary to `TuningConfig` covering all setups with asymmetric bullish/bearish parameters.
   - Configure `gate_counter_trend_fades: bool = True` in `TuningConfig`.
   - Configure `bullish_continuation_volume_ratio: float = 1.4` (versus 1.2 for bearish).

2. **`engine.py`**:
   - Update `apply_per_type_levels()` to resolve `f"{signal.setup_type.value}_{signal.direction.value}"` from `settings.directional_levels` before falling back to `settings.per_type_levels`.
   - In `AresEngine.tick()`, add the counter-trend fade gate: suppress Bullish `OI_WALL_REJECTION` and `FAILED_BREAKOUT` when `is_downtrend` is True.

3. **`detectors/continuation.py`**:
   - In `TrendContinuationDetector.update()`, use `settings.bullish_continuation_volume_ratio` when evaluating bullish resumption volume.

4. **`tests/unit/test_manm159_performance_asymmetry.py`**:
   - Add unit tests verifying:
     a) `apply_per_type_levels` assigns distinct directional targets for Bullish vs Bearish trend continuation (e.g. T2=50 for Bullish vs T2=80 for Bearish).
     b) Counter-trend bullish fades (`OI_WALL_REJECTION`, `FAILED_BREAKOUT`) are suppressed when `is_downtrend` is active.
     c) Bullish `EXHAUSTION_REVERSAL` signals are **not** suppressed and pass through cleanly.
     d) Bullish continuation requires the configured higher volume ratio.
     e) Verify 100% test suite pass rate across all existing engine and detector tests.

---

## 5. Definition of Done & Acceptance Criteria

- [ ] Statistical analysis (bootstrap confidence intervals, trimmed/winsorized means, outlier sensitivity, setup cross-tabs, option metrics) documented in ADR.
- [ ] Root cause identified: Macro regime bias (-833 pt downtrend), extreme right-tail outlier dependency in bearish profits, toxic counter-trend fades (`OI_WALL_REJECTION` & `FAILED_BREAKOUT`), and symmetric target mismatch.
- [ ] Asymmetrical level resolution implemented in `config_profiles.py` and `engine.py`.
- [ ] Counter-trend fade gate implemented in `engine.py` (`gate_counter_trend_fades`).
- [ ] Bullish resumption confirmation strengthened in `detectors/continuation.py`.
- [ ] Unit tests added in `tests/unit/test_manm159_performance_asymmetry.py` covering all new behaviors.
- [ ] 100% of existing tests pass (`pytest`).
- [ ] No regression to existing Bearish strategy profitability.
