# Trend Expansion Strategy — Production Specification & Validation

**Author:** ARES Engineering  
**Implementation:** [`detectors/trend_expansion.py`](file:///Users/manmadeanyme/Documents/Work/ARES/detectors/trend_expansion.py) | [`engine.py`](file:///Users/manmadeanyme/Documents/Work/ARES/engine.py)  
**Tests:** [`tests/unit/test_trend_expansion_detector.py`](file:///Users/manmadeanyme/Documents/Work/ARES/tests/unit/test_trend_expansion_detector.py)  
**Status:** Production signal path (`SIGNAL_REFERENCE` mode; manual references only)

---

## 1. Overview

The **Trend Expansion Strategy** targets vertical trend days and short-squeeze regimes where mean-reversion and shallow continuation pullbacks underperform. It operates as the 5th strategy in `AresEngine`, evaluating after failed-breakout and OI-wall checks, and before standard continuation/exhaustion detectors.

Two complementary setups are evaluated:
1. **`VWAP_RECLAIM` (10:30 – 12:30 IST):** Higher-low value reversal following a 35%–68% Fibonacci liquidity sweep below VWAP.
2. **`AFTERNOON_BREAKOUT` (12:30 – 14:15 IST):** Range expansion breakout above morning consolidation (09:15–12:30 IST) with volume and Bollinger Bandwidth participation.

All windows use candle-start timestamps and exclude their end boundary. The 12:30 candle is eligible as a breakout candle but is not part of the range anchor; the last eligible breakout candle starts at 14:14 and completes by 14:15.

---

## 2. Multi-Factor Regime Gate

Evaluated at or after **10:30 IST**. Requires at least **2 of 4** volatility factors to qualify the session:

| Factor | Metric | Default Threshold (`TuningConfig`) |
|---|---|---|
| **1. Overnight Gap** | $\|\text{Open}_{09:15} - \text{PDC}\|$ | $\ge 70.0$ pts |
| **2. Opening Range Width** | $\text{High}_{09:15\text{-}09:45} - \text{Low}_{09:15\text{-}09:45}$ | $\ge 85.0$ pts |
| **3. VWAP Dispersion** | $\sigma_{VWAP} = \sqrt{E[TP^2] - (E[TP])^2}$ | $\ge 25.0$ pts |
| **4. Directional Displacement** | $\|\text{Close}_{09:45} - \text{Open}_{09:15}\|$ | $\ge 45.0$ pts |

*Fail-Closed Invariant:* Missing PDC or a broken 1-minute candle stream from 09:15 through the 10:30 regime check disqualifies the session (`allow_signal = False`). If the startup PDC fetch fails, the live loop retries once at the regime check before failing closed.

---

## 3. Setup Rules & Geometry

### Setup A: `VWAP_RECLAIM`
- **Window:** 10:30 – 12:30 IST.
- **Impulse Filter:** Morning leg ($\text{High}_{morning} - \text{Low}_{morning}$) $\ge 70$ pts.
- **Sweep Boundary:** Pullback reaches the configured VWAP buffer, bounded by $0.35 \le \frac{\text{High}_{morning} - \text{Low}_{sweep}}{\text{Impulse}} \le 0.68$, holding above the session low.
- **Trigger:** 1-minute close crosses back above computed session VWAP with $\text{EMA}_9 \ge \text{EMA}_{20}$.
- **Stop Loss:** $\text{Low}_{sweep} - 3.0$ pts; signals are rejected unless entry risk is between 15 and 35 pts.
- **Target 1 (T1):** Retest of morning high ($\text{High}_{morning}$).
- **Target 2 (T2):** Nearest eligible structural level beyond T1, or the configured reference fallback.

### Setup B: `AFTERNOON_BREAKOUT`
- **Window:** 12:30 – 14:15 IST.
- **Anchor:** Range high/low formed between 09:15 and 12:30 IST.
- **Trigger:** 1-minute close $> \text{Range High} + 2.0$ pts with:
  - $\text{Close} > \text{VWAP}$ and $\text{Close} > \text{EMA}_{20}$
  - $Z_{VWAP} = \frac{\text{Close} - \text{VWAP}}{\sigma_{VWAP}} \ge 0.8$
  - $\text{Volume} \ge 0.95 \times \text{SMA}_{20}(\text{Volume})$
  - $\text{BBW Expansion} = \frac{\text{BBW}}{\text{SMA}_{20}(\text{BBW})} \ge 1.0$
- **Stop Loss:** The higher of the preceding 20-candle base low and entry minus 35 pts; signals are rejected if risk is below 12 pts.
- **Target 1 (T1):** $\text{Entry} + 35.0$ pts.
- **Target 2 (T2):** Nearest eligible structural level beyond T1, or the configured reference fallback.
- **IV Gate:** When `trend_expansion_iv_filter_enabled` is on, missing/invalid ATM Call IV or a calibrated vol-crush/climax reading suppresses the breakout. IV observations are isolated by strike to avoid treating ATM rolls as IV changes.

The IV gate applies to `AFTERNOON_BREAKOUT` only. `VWAP_RECLAIM` remains governed by its existing price, VWAP, and EMA rules.

---

## 4. Lifecycle & Risk Invariants

1. **Signal-only boundary:** This detector emits manual spot-signal references and submits no broker orders. It does not own position exits, EOD square-off, or a strategy-specific trailing stop.
2. **Session state:** Missing prior-day close or an incomplete 09:15-starting candle stream fails the regime gate closed for that session.
3. **Sweep state:** Transient sweep state (`_sweep_low`, `_dipped_below_vwap`) is reset when a reclaim is emitted or suppressed, or when the sweep is invalidated. Registered-trade lifecycle follows the system's shared position-management policy.

---

## 5. Telemetry & Live Signal Integration

- **Execution Mode:** Emits manual `SIGNAL_REFERENCE` alerts. Does not submit paper or live broker orders.
- **Persistence:** Stored in Supabase `ares_signals` with `setup_type = "TREND_EXPANSION_RECLAIM"` or `"TREND_EXPANSION_BREAKOUT"`; `market_context.trend_expansion_setup` records `"VWAP_RECLAIM"` or `"AFTERNOON_BREAKOUT"`.
- **Config Toggle:** Configured in `TuningConfig` under `trend_expansion_enabled` (default `True` in both profiles); strategy thresholds and setup parameters are the `trend_expansion_*` profile fields.
