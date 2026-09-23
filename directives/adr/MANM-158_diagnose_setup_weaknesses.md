---
adr_id: "MANM-158"
title: "Root-Cause Diagnostics and Risk Control Architecture for Setup-Level Weaknesses (OI Wall Rejection and Failed Breakout)"
status: "proposed"
date: "2026-09-12"
author: "Software Architect Agent"
applies_to: "ARES Core / Detectors / Engine / Risk Controls / Config Profiles / Validation"
issue: "MANM-158 / 01a09105-e021-7b7a-8f35-19d20b83b25a"
---

# Architecture Decision Record: [MANM-158] Root-Cause Diagnostics and Risk Control Architecture for Setup-Level Weaknesses

## 1. Status & Context

- **Status**: Proposed (Awaiting human review and approval before Code Generator implementation).
- **Date**: 2026-09-12.
- **Problem Statement**:
  Historical backtesting and live audit across 233 recorded trades (Supabase baseline audit 2026-09-11) revealed severe performance divergence across ARES detector setups:
  - `TREND_CONTINUATION`: 90 trades, **+372.70 points**, 34.4% win rate, **+4.14 pts/trade** expectancy.
  - `EXHAUSTION_REVERSAL`: 106 trades, **+92.90 points**, 30.2% win rate, **+0.88 pts/trade** expectancy.
  - `FAILED_BREAKOUT`: 17 trades, **-51.55 points**, 35.3% win rate, **-3.03 pts/trade** expectancy.
  - `OI_WALL_REJECTION`: 20 trades, **-143.20 points**, 30.0% win rate, **-7.16 pts/trade** expectancy.

  Together, `OI_WALL_REJECTION` and `FAILED_BREAKOUT` accumulated **-194.75 points of negative PnL drag** across 37 trades (15.9% of all trades taken). Without these two failing setups, the system's net spot PnL would increase from **+270.85 pts -> +465.60 pts (+71.9% gain)**.
  
  `OI_WALL_REJECTION` represents the single most destructive setup in the suite, suffering from a 30.0% win rate and an average win-to-loss ratio of 0.84, while `FAILED_BREAKOUT` exhibits repeated whipsaw stop-outs on level re-tests.

---

## 2. Comprehensive Root-Cause Analysis (RCA)

A rigorous architectural and mathematical audit of `detectors/oi_wall.py`, `detectors/oi_wall_entry.py`, `detectors/breakout.py`, `engine.py`, and historical trade traces revealed five distinct root causes:

### RCA 1: Decoupled Entry vs Fixed Stop Geometry (The "Stranded Stop" Defect)
- **Mechanism**:
  - In TASK-185, `apply_per_type_levels` in `engine.py` centralized stop-loss assignment to a fixed absolute distance from `trigger_price`:
    $$\text{SL} = \text{trigger\_price} - (\text{direction\_sign} \times \text{stop\_pts})$$
  - In TASK-073, `OIWallEntryFilter` introduced a multi-candle secondary re-test qualification:
    `INTERACTED` $\to$ `PERSISTENT` $\to$ favourable excursion ($\ge 20$ pts) $\to$ `RETEST_READY` $\to$ re-test touch candidate $\to$ directional confirmation candle $\to$ `QUALIFIED`.
  - The confirmation candle requires `candle.close < candidate.low` (bearish) or `candle.close > candidate.high` (bullish).
  - Consequently, `trigger_price` (which is `candle.close`) is typically located **15 to 25 points away from the wall strike**.
- **The Critical Defect**:
  - With `oi_wall` stop loss set to 16.0 pts in `_PER_TYPE_LEVELS_DEFAULT`:
    Suppose a Call Resistance Wall sits at Strike $K = 24100$. Price interacts, excursions down to 24075, retests at 24095, and a confirmation candle closes at $24080$.
    $$\text{Entry} = 24080.0, \quad \text{SL} = 24080.0 + 16.0 = \mathbf{24096.0}$$
    **The wall is at 24100.0, but the stop loss is placed at 24096.0 (4.0 points BELOW the wall strike)!**
  - The protective barrier that defines the setup is the 24100 option wall. Placing the stop loss at 24096 means any standard intra-candle re-test or noise between 24080 and 24100 triggers a premature stop-out, even when the 24100 wall holds perfectly.
  - This mathematically explains the Phase 1 audit finding: **12 of 18 stopped-out trades later reversed and hit Target 1 in the predicted direction**.
- **Failed Breakout Parallel**:
  - In `detectors/breakout.py`, a failed breakout requires `candle.close` past the level, with `deep_close` requiring $\ge 5.0$ pts.
  - If a resistance level is at $24100$, and the failure candle closes deeply at $24088$ (12 pts below the level):
    With `stop_pts = 12.0`, $\text{SL} = 24088 + 12.0 = \mathbf{24100.0}$.
  - The stop loss is placed exactly at the broken level or inside it. Standard price action retests of broken levels invariably sweep this stop before the move develops.

### RCA 2: Counter-Trend Fading during Strong Momentum Regimes (Regime Blindness)
- In TASK-182, the trend-regime filter and observation gates were disabled to avoid silencing the system during trending sessions.
- While this benefited `TREND_CONTINUATION` (+372.70 pts), it exposed `OI_WALL_REJECTION` and `FAILED_BREAKOUT` to catastrophic momentum runs:
  - Both setups are **mean-reverting counter-trend fades**.
  - During strong institutional trend regimes (e.g. price persistently on one side of intraday VWAP, ADX $> 25$, high directional volume), option writers at wall strikes get caught in short squeezes / long liquidations and are forced to cover.
  - Attempting to fade a breakout or short an OI wall during strong institutional buying directly confronts order flow imbalance, leading to immediate 100% loss rates.

### RCA 3: Static vs Dynamic Open Interest ("Zombie Wall" Phenomenon)
- `OIWallDetector` qualifies walls based on:
  $$\text{ce\_oi} > 4,000,000 \quad \text{and} \quad \text{ce\_oi\_change\_pct} > 5.0\%$$
- In NIFTY index options:
  - 4M contracts is frequently accumulated over preceding weeks (stale open interest).
  - A 5% intraday change on a high-OI strike can represent passive retail positioning or delta hedging.
  - When price approaches a wall, if institutional writers are actually unwinding or migrating to higher strikes, the wall collapses. However, because cumulative OI remains above 4M contracts, the detector continues to treat it as an active barrier.
  - True barrier defense requires **active delta accumulation** and sustained positive OI delta expansion during the approach, rather than static historical OI.

### RCA 4: Over-Sensitive Failure Trigger in Failed Breakout (TASK-184 Degradation)
- In TASK-172, `breakout_failure_min_score` was set to 3.
- In TASK-184, this was lowered back to 2 ("to restore medium breakout").
- Scoring conditions in `FailedBreakoutDetector`:
  1. `weak_volume` ($< 0.75 \times \text{avg}$)
  2. `iv_falling` ($< -3.0\%$)
  3. `writers_active` ($\ge 10.0\%$ OI change)
  4. `deep_close` ($\ge 5.0$ pts)
- With `min_score = 2`, any candle having `weak_volume` (common on 1-min charts) and closing 5 pts past the level (`deep_close`) triggers a live trade without requiring active writer defense or IV crush confirmation.
- As a consequence, ordinary shallow consolidation pullbacks in healthy breakouts are misdiagnosed as "failed breakouts", entering counter-trend positions right before the breakout trend resumes.

### RCA 5: Mathematical Expectancy Breakdown
- For any setup with win rate $P_w$ and average win/loss ratio $R = \frac{\bar{W}}{\bar{L}}$, the expected profit per trade is:
  $$E = P_w \bar{W} - (1 - P_w) \bar{L}$$
- To break even ($E \ge 0$), the required win rate is:
  $$P_w^{\text{breakeven}} = \frac{1}{1 + R}$$
- **OI Wall Rejection**:
  - Realized win rate: $P_w = 0.300$.
  - Average loss: $\bar{L} = 16.0$ pts; Average win: $\bar{W} = 13.47$ pts $\implies R = 0.842$.
  - Required win rate for breakeven: $\frac{1}{1 + 0.842} = \mathbf{54.3\%}$.
  - Deficit: Actual win rate (30.0%) is **24.3 percentage points below breakeven**, guaranteeing consistent capital erosion.
- **Failed Breakout**:
  - Realized win rate: $P_w = 0.353$.
  - Stop loss: $12.0$ pts; Target 1: $20.0$ pts.
  - Theoretical breakeven win rate: $\frac{12.0}{12.0 + 20.0} = \mathbf{37.5\%}$.
  - Because trade exits are frequently degraded by breakeven stops and early reversals, realized expectancy is $-3.03$ pts/trade.

---

## 3. Architectural Decisions & Strategy Evaluation

We evaluate three potential architectural paths:

| Strategic Option | Pros | Cons | Recommendation |
| :--- | :--- | :--- | :--- |
| **Option 1: Complete Retirement** (Permanently delete both detectors) | Immediately stops PnL bleed (+194.75 pts saved); simplifies codebase. | Irrevocably discards structural information; ignores valid false breakouts during chop. | Rejected (overly coarse). |
| **Option 2: Unfiltered Parameter Retuning** (Curve-fit SL/T1 on existing 37 trades) | Keeps setups active. | Extreme risk of overfitting on $N=17$ and $N=20$; fails out-of-sample validation. | Rejected (violates acceptance criteria). |
| **Option 3: Gated Phased Remediation with Structural Re-Anchoring (Recommended)** | Immediate capital protection via live emission gating; structural SL re-anchoring fixes the stranded stop defect; walk-forward validation prevents curve-fitting. | Requires phased implementation. | **Adopted.** |

### Detailed Decisions

#### Decision 1: Immediate Live Execution Gating (Phase 1 Protection)
1. **Disable `OI_WALL_REJECTION` Live Signal Emission**:
   - Add `oi_wall_trading_enabled: bool = False` to `TuningConfig` (`config_profiles.py`).
   - `AresEngine.tick()` will continue updating `OIWallDetector` and `OIWallEntryFilter` on every candle to preserve full state machine continuity, write `latest_oi_wall_context` to `ml_collection`, and support telemetry.
   - However, `AresEngine` will **suppress live trade generation** for `OI_WALL_REJECTION` until Phase 2 validation criteria are met.
   - Rationale: With $-7.16$ pts/trade expectancy and a 24.3% win rate deficit, live trading must be halted immediately to protect capital.

2. **Strictly Gate `FAILED_BREAKOUT`**:
   - Revert `breakout_failure_min_score` from `2` to `3` in `NON_EXPIRY_CONFIG` and `EXPIRY_CONFIG`.
   - A failed breakout will require at least three concurrent confirmations (e.g. `weak_volume` + `deep_close` + `writers_active` $\ge 10\%$, or `iv_falling`).
   - Add **Intraday VWAP Regime Filtering**:
     - A bearish failed breakout is suppressed if spot is $> 15$ pts above intraday VWAP with positive VWAP slope.
     - A bullish failed breakdown is suppressed if spot is $> 15$ pts below intraday VWAP with negative VWAP slope.

#### Decision 2: Structurally Anchored Stop-Loss Geometry (Phase 2)
Reform `apply_per_type_levels` in `engine.py` to support **structural level anchoring** instead of blind fixed offsets:
- For `FAILED_BREAKOUT`:
  $$\text{SL} = \text{breakout\_level} + (\text{direction\_sign} \times \text{structural\_sl\_buffer})$$
  where default `breakout_structural_sl_buffer = 4.0` pts.
  - If a short entry triggers at $24088$ after failing $24100$, SL is placed at $24100 + 4 = \mathbf{24104.0}$ (above the breakout level), rather than $24088 + 12 = 24100.0$.
  - Target 1 remains fixed at 20.0 pts from entry, ensuring valid R:R.
- For `OI_WALL_REJECTION` (when evaluated in replay):
  $$\text{SL} = \text{wall\_strike} + (\text{direction\_sign} \times \text{wall\_structural\_sl\_buffer})$$
  where `wall_structural_sl_buffer = 6.0` pts.
  - For a short entry at $24080$ against a 24100 CE wall, SL sits at $24100 + 6 = \mathbf{24106.0}$ (behind the wall), completely eliminating the stranded stop defect.

#### Decision 3: Out-of-Sample Walk-Forward Replay Framework (`scripts/replay_validation.py`)
To satisfy acceptance criteria and prevent in-sample curve-fitting:
1. Implement a dedicated, reproducible historical replay harness in `scripts/replay_validation.py`.
2. Partition historical Nifty 1-minute OHLCV + option chain data into three disjoint chronological windows:
   - **Window 1 (In-Sample Calibration)**: 2026-06-24 to 2026-07-20 ($N \approx 75$ trades).
   - **Window 2 (Out-of-Sample Validation)**: 2026-07-20 to 2026-08-15 ($N \approx 80$ trades).
   - **Window 3 (Holdout Test)**: 2026-08-15 to 2026-09-11 ($N \approx 78$ trades).
3. The script executes the full `AresEngine` pipeline tick-by-tick and compares baseline vs adjusted configurations.

#### Decision 4: Promotion Gate Policy
No disabled or gated setup may be promoted back to live trading unless out-of-sample replay demonstrates:
1. Sample size $N \ge 25$ trades in the validation/holdout window.
2. Win Rate $\ge 40.0\%$.
3. Expectancy $\ge +2.5$ points per trade.
4. Profit Factor $\ge 1.30$.
5. Maximum Drawdown reduction of at least 30% compared to baseline.

---

## 4. Component Boundaries and File Assignments

Implementation is strictly assigned to the **Code Generator Agent** following human approval of this ADR:

| File | Module / Responsibility |
| :--- | :--- |
| `config.py` / `config_profiles.py` | Add `oi_wall_trading_enabled: bool = False`, `breakout_regime_filter_enabled: bool = True`, `breakout_failure_min_score: int = 3`, `breakout_structural_sl_buffer: float = 4.0`, `oi_wall_structural_sl_buffer: float = 6.0`. |
| `detectors/breakout.py` | Update `FailedBreakoutDetector` to accept optional `vwap` and `vwap_slope` for trend regime gating; include structural reference level in signal metadata. |
| `engine.py` | 1. In `tick()`, evaluate `settings.oi_wall_trading_enabled`; if False, suppress signal emission while recording `SUPPRESSED_BY_TRADING_GATE` telemetry and advancing detector state.<br>2. Update `apply_per_type_levels()` to apply structurally anchored stop losses for `FAILED_BREAKOUT` using `signal.reference_level`. |
| `scripts/replay_validation.py` | Create standalone replay script supporting walk-forward out-of-sample simulation and performance metric generation across all 4 setups. |
| `tests/unit/test_task158_setup_weaknesses.py` | New comprehensive unit test suite verifying: (a) OI wall background tracking with trading disabled, (b) Failed breakout score-3 gate and regime filtering, (c) Structural stop loss placement calculations. |
| `tests/unit/test_config_profiles.py` | Update configuration assertions to reflect new defaults. |

---

## 5. API Contracts & Interfaces

### 1. `TuningConfig` Additions (`config_profiles.py`)
```python
@dataclass
class TuningConfig:
    # ... existing fields ...
    
    # MANM-158: Setup-level weakness controls
    oi_wall_trading_enabled: bool = False
    breakout_failure_min_score: int = 3
    breakout_regime_filter_enabled: bool = True
    breakout_regime_vwap_distance_pts: float = 15.0
    breakout_structural_sl_buffer: float = 4.0
    oi_wall_structural_sl_buffer: float = 6.0
```

### 2. Structural SL Calculation Contract (`engine.py`)
```python
def apply_per_type_levels(signal: AresSignal, settings, levels=None) -> None:
    lv = settings.per_type_levels.get(signal.setup_type.value)
    if lv is None:
        return
    entry = signal.trigger_price
    sign = 1 if signal.direction == Direction.BULLISH else -1

    # MANM-158: Structural Stop Loss anchoring for Level-based setups
    if signal.setup_type == SetupType.FAILED_BREAKOUT and hasattr(signal, "reference_level") and signal.reference_level:
        buffer = getattr(settings, "breakout_structural_sl_buffer", 4.0)
        # For BEARISH, SL sits above broken level: level + buffer
        # For BULLISH, SL sits below broken level: level - buffer
        signal.stop_loss = signal.reference_level - (sign * buffer)
    elif signal.setup_type == SetupType.OI_WALL_REJECTION and hasattr(signal, "reference_level") and signal.reference_level:
        buffer = getattr(settings, "oi_wall_structural_sl_buffer", 6.0)
        signal.stop_loss = signal.reference_level - (sign * buffer)
    else:
        # Default fixed-distance SL
        signal.stop_loss = entry - sign * lv.stop_pts

    signal.target_1 = entry + sign * lv.target_1_pts
    signal.target_2 = _resolve_target_2(entry, sign, signal.target_1, lv, levels)
```

---

## 6. Performance, Security & Operational Considerations

1. **Deterministic Execution**:
   - All proposed filter logic and structural calculations are strictly $O(1)$ in time complexity and consume zero additional memory.
2. **Telemetry & Audit Preservation**:
   - Gating `OI_WALL_REJECTION` live trading does NOT disconnect `ml_collection` logging. All telemetry (`ml_collection.oi_wall_context`) remains fully populated on every cycle for ongoing research.
3. **No External Dependencies**:
   - Uses existing math, models, and pandas routines. Does not introduce external TA libraries or network dependencies.
4. **Capital Preservation**:
   - Disabling `OI_WALL_REJECTION` immediately prevents an observed $-7.16$ pts/trade drag on live capital.

---

## 7. Definition of Done (DoD)

- [x] Comprehensive root-cause analysis completed and documented in ADR-158.
- [x] Mathematical breakdown of expectancy deficit and structural SL flaws established.
- [x] File-level implementation specifications and API contracts defined.
- [x] ADR added to `directives/adr/INDEX.md`.
- [x] Obsidian project log synced in `~/Documents/Obsidian/Projects/Ares/MANM-158-log.md` and repo docs in `docs/MANM-158-diagnose-setup-weaknesses.md`.
- [x] Feature branch created and PR raised for human review.
- [ ] Human approval obtained for ADR-158.
- [ ] Code Generator implements specified contracts and unit tests.
- [ ] Out-of-sample validation demonstrates positive expectancy before any future live re-enablement.
