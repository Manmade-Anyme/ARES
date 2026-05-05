# Orchestration Layer (Engine)

The `AresEngine` (located in `engine.py`) is the core orchestration unit of ARES. It executes on every 1-minute cycle tick.

## Responsibilities

1. **State Buffering:** Maintains rolling buffers for volume (`candle_buffer`) and Implied Volatility (`iv_buffer`). These buffers are essential for calculating relative changes (like volume climaxes and IV crushes).
2. **Signal Cooldown Enforcement:** To prevent spam in choppy markets, it strictly enforces a cooldown period (e.g., 5 minutes) after a signal is generated.
3. **Detector Orchestration:** Evaluates the [[05_Detectors]] in a strict, short-circuited priority order.

## Priority Order Rationale

Higher confidence setups are checked first. If a signal is found, the evaluation short-circuits.

1. **Failed Breakout** (Highest Precision)
2. **OI Wall Rejection** (High Precision)
3. **Exhaustion Reversal** (Medium Precision)

## `tick()` Execution Flow
1. **Update Buffers:** Appends the latest candle and ATM CE IV.
2. **Cooldown Check:** Returns `None` if the cooldown hasn't expired.
3. **Calculate Averages:** Calculates the average volume from the rolling buffer.
4. **Get Previous IV:** Extracts the previous IV for momentum comparison.
5. **Run Detectors:** Executes detectors with the `or` operator to short-circuit upon finding a signal.
6. **Set Cooldown:** If a signal is found, updates `last_signal_time`.
7. **Return:** Returns the `AresSignal` or `None`.
