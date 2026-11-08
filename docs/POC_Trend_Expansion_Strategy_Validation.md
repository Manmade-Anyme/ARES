# Trend Expansion Strategy — Production Specification

**Implementation:** [detectors/trend_expansion.py](../detectors/trend_expansion.py), [engine.py](../engine.py)
**Tests:** [tests/unit/test_trend_expansion_detector.py](../tests/unit/test_trend_expansion_detector.py)
**Execution:** Manual signal references only; this detector does not submit broker orders.

## 1. Evaluation schedule

The detector evaluates each completed candle throughout the regular market session, **09:15–15:30 IST**. There is no 10:30 regime check, morning-only reclaim window, afternoon breakout window, or 14:15 entry cutoff. The session boundary is the only time-of-day gate.

The regime is recalculated on every live candle. Its rolling price factors use up to the latest 25 candles. A fresh detector needs 25 observed candles for a complete setup range; this is indicator warm-up, not a clock-time entry window. If the process starts mid-session, it builds that range from candles it observes after startup.

## 2. Rolling regime gate

At least **2 of 4** current factors must qualify:

| Factor | Metric | Default threshold |
|---|---|---:|
| Prior-close extension | Absolute distance between latest close and verified prior-day close | 70 pts |
| Rolling range expansion | High minus low across up to the latest 25 candles | 85 pts |
| VWAP dispersion | Session volume-weighted typical-price standard deviation | 25 pts |
| Directional displacement | Absolute distance from first open to last close in the rolling 25-candle window | 45 pts |

Missing or invalid prior-day close keeps the regime gate closed. The live loop retries the prior-day context fetch on its first live candle if startup fetch did not provide a close. Price data must be monotonic; a missing minute does not disable the rest of the session.

## 3. Setups

### `VWAP_RECLAIM`

- Uses the preceding 25 candles to form a rolling impulse range.
- Requires impulse width of at least 70 pts and a high above the first candle's open.
- Tracks a pullback through the configured VWAP buffer, while keeping the sweep above the rolling range low.
- Requires retracement between 35% and 68%, a close back above session VWAP, and EMA9 at or above EMA20.
- Stop is the sweep low minus the configured buffer; risk must remain within the configured reclaim limits.
- Target 1 is the rolling impulse high.
- This setup has no IV gate; its existing price, VWAP, EMA, and risk conditions remain in force.

### `ROLLING_BREAKOUT`

- Compares the current close with the high of the preceding 25 candles; it does not use a fixed opening-range anchor.
- Requires a close above that range plus the configured buffer, above VWAP and EMA20, and a qualifying VWAP z-score, volume ratio, and Bollinger Bandwidth expansion.
- When enabled, the IV gate requires valid ATM Call IV and fresh same-strike samples near both one and three minutes earlier. Missing or stale comparison data suppresses the signal (`NO_IV_DATA`); a different strike is never used as a fallback.
- Stop and target geometry remain configured by the detector and shared signal lifecycle.

Both setups are evaluated during the regular market session whenever their rolling data and regime conditions qualify. Signal count remains capped by `trend_expansion_max_trades_per_day`.

## 4. Lifecycle and boundaries

1. The detector resets state on a new local market date and rejects duplicate or out-of-order candles.
2. No signal is evaluated before 09:15 or at/after 15:30 IST.
3. Missing prior-day close suppresses signals; missing/invalid IV suppresses only `ROLLING_BREAKOUT` when its IV filter is enabled.
4. Signals are persisted as `TREND_EXPANSION_RECLAIM` or `TREND_EXPANSION_BREAKOUT`. The breakout context is now `ROLLING_BREAKOUT`; older stored signals may still contain the former `AFTERNOON_BREAKOUT` label.
5. This detector emits manual references only. It does not own order submission, position exits, or end-of-day square-off.
