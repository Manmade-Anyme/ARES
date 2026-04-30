# Triage Brief — Bug Sweep
**Severity:** medium
**Triggered by:** Human request

## Observed Behaviour
1. **Markdown Formatting Error:** The `send_startup_alert` Discord payload had a malformed closing code block (`+ ````) which fails rendering in Discord.
2. **Error Spam / Alert Fatigue:** When the Dhan API returns an empty option chain response (e.g., during off-market hours or API glitches), a `ValueError` is triggered. This caused an identical error alert to fire via Discord webhook every 60 seconds.
3. **Dynamic Target Null Check:** Evaluated the risk of a `TypeError` in `detectors/breakout.py` and `exhaustion.py` when `target_2` is `None` but a subtraction operation `abs(target_2 - candle.close)` exists on the same line. 

## Expected Behaviour
1. Discord Markdown blocks should close cleanly without diff prefixes.
2. The system should deduplicate identical consecutive errors so Discord isn't spammed with `Fetch cycle error - Invalid option chain response`.
3. The system should handle `target_2 = None` safely without crashing.

## Known / Unknown
**Known:**
- `alerts.py` line 66 contained the `+ ```` syntax.
- `main.py` did not implement any rate limiting or deduplication for error alerts.
- In Python, `True or X` short-circuits. Thus `not target_2 or abs(...)` safely skips the right-side evaluation when `target_2` is `None`, preventing the `TypeError`.

## Minimal Reproduction Steps
1. Restart the system — observe the Discord startup alert (diff formatting may break in some clients).
2. For error spam: Mock the `oi_fetcher.py` to raise a `ValueError`, and watch Discord receive 1 notification per minute.

## Delegation
→ Debug Agent / Code Generator (Already executed inline)
- Fixed `alerts.py` Markdown syntax.
- Added `last_error_msg` state to the `while True:` loop in `main.py` to silence duplicate consecutive alerts.
