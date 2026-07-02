# Exhaustion Detector Improvement Todo

## Objective
Upgrade the exhaustion reversal detector so that it does not fire on a single doji-like candle with a volume spike alone. Instead, it should require confirmation from the next 1-minute candle and use volume as a confirmation filter, not just a trigger.

## Why this change
The current detector in [detectors/exhaustion.py](detectors/exhaustion.py) can generate a signal from a single candle that is merely doji-like and high volume. That made the recent bearish signal too early and too weak, and it was stopped out quickly. The goal is to reduce weak or premature trades and improve signal quality.

## Desired behavior
A bearish exhaustion signal should only be generated when:
- the current candle is doji-like,
- volume is climactic,
- the pattern occurs near a meaningful structural level if available,
- and the next 1-minute candle confirms the reversal by breaking below the first candle’s low (or above its high for bullish setups).

A bullish exhaustion signal should follow the mirrored logic.

## Implementation scope
1. Update the exhaustion detector logic in [detectors/exhaustion.py](detectors/exhaustion.py)
2. Add or adjust tuning parameters in [config_profiles.py](config_profiles.py)
3. Add regression tests in [tests/unit/test_exhaustion.py](tests/unit/test_exhaustion.py)
4. Keep the rest of the engine flow unchanged unless required for state handling

## Proposed changes
### 1) Add confirmation candle logic
Modify the detector so it does not emit a signal immediately on the initial doji-like candle.

Instead:
- store the candidate exhaustion setup temporarily,
- wait for the next candle,
- confirm only if the next candle closes in the expected direction and meets the volume/price criteria.

For a bearish setup:
- Candle 1: doji-like + high volume + near level
- Candle 2: closes below Candle 1 low

For a bullish setup:
- Candle 1: doji-like + high volume + near level
- Candle 2: closes above Candle 1 high

### 2) Strengthen volume handling
Volume should be used as both:
- an initial trigger condition, and
- a confirmation condition.

Suggested logic:
- initial candle: volume > threshold multiplier (e.g. 3.0x average)
- confirmation candle: volume remains elevated or above average, preferably with a stronger breakout rejection bar

### 3) Make structural level usage explicit
Use a structural level only if the candle is near it within a reasonable threshold (for example 8–10 points). This keeps the setup from firing randomly in a flat market.

### 4) Keep confidence scoring but raise the bar for HIGH
The current confidence scoring should remain, but the threshold to generate HIGH should become stricter. MEDIUM should remain the default if only the initial conditions are met.

## Suggested configuration changes
In [config_profiles.py](config_profiles.py), consider:
- increasing the volume multiplier from 2.5 to 3.0 or 3.5
- tightening the doji body ratio threshold from 0.35 to 0.25 or 0.20
- adding a minimum candle range threshold (for example 15–20 points)

## Tests to add
Add tests that cover:
1. no signal when the initial doji candle is present but the next candle does not confirm
2. signal generated when the next candle confirms bearish exhaustion
3. signal generated when the next candle confirms bullish exhaustion
4. signal suppressed when volume is not elevated enough on the confirmation candle
5. confidence remains MEDIUM unless extra conditions are met

## Prompt to use for implementation
Use the following prompt with the coding agent:

"Implement a confirmation-based exhaustion reversal detector change in the ARES repo.

Context:
- The current exhaustion detector in [detectors/exhaustion.py](detectors/exhaustion.py) fires on a single doji-like candle with a volume climax. This is too early and has produced weak bearish signals that were stopped out quickly.
- The trading logic currently pulls 1-minute data already, so waiting one extra minute for confirmation is practical and should improve signal quality.
- The detector should not emit a signal immediately on the first candle. Instead, it should track a candidate exhaustion pattern and require confirmation from the next candle.
- For bearish setups: the first candle should be doji-like with high volume near a structural level, and the next candle should confirm by closing below the first candle's low.
- For bullish setups: mirror this logic with the next candle closing above the first candle's high.
- Volume should be used as both an initial trigger and a confirmation filter.
- Keep the rest of the engine flow unchanged unless necessary for state handling.
- Add or update tests in [tests/unit/test_exhaustion.py](tests/unit/test_exhaustion.py) to cover both confirmed and unconfirmed cases.
- Update tuning parameters in [config_profiles.py](config_profiles.py) if needed to make the detector stricter.

Please implement the change and verify it with the relevant unit tests."

## Acceptance criteria
- The detector no longer fires on a single doji-like candle alone.
- A confirmed exhaustion setup requires a following candle that validates the reversal.
- Weak signals are reduced and the detector becomes more selective.
- Unit tests cover both confirmed and rejected cases.
