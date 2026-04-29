# TASK-002 Generic Asset Configuration
**Date:** 2026-04-28
**Status:** ready

## Goal
Decouple the application from strict Nifty-50 variables, allowing any generalized instrument.

## Inputs
- `.env` holding old var names.
- `config.py` hardcoding names.
- `price_fetcher.py` and `oi_fetcher.py` using `nifty_` pre-text variables.

## Expected Output
- `.env` updated with generic equivalents.
- `config.py` renamed to `security_id` and `exchange_segment`. Include `yahoo_symbol` and `instrument_type`.
- Fetch modules refer to generalized variables dynamically.

## Acceptance Criteria
- User running `python main.py` triggers an asset fetch dynamically matching the `yahoo_symbol` set in `.env` without any reference to NIFTY internal var mappings.

## Edge Cases
- Make sure to add `INSTRUMENT_TYPE` so that `INDEX`, `FUTCOM` and `OPTIDX` are flexibly allowed through DhanHQ wrapper.
