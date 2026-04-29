# TASK-001 Dynamic PDH/PDL 
**Date:** 2026-04-28
**Status:** ready

## Goal
Automate the PDH (Previous Day High) and PDL (Previous Day Low) boundaries to eliminate manual value updates before daily execution of the swing trading system.

## Inputs
- Current implementation in `main.py` initializes them explicitly.
- We have the `fetchers/price_fetcher.py` and `dhanhq` with `httpx` readily installed.

## Expected Output
- A new asynchronous method on `PriceFetcher` internally (e.g. `fetch_previous_day_ohlc`).
- `main.py` initialization calls the fetch method instead of hardcoded assignment.
- Robust unit tests ensuring correct parsing of the daily highs and lows.

## Acceptance Criteria
- Starting the script via `python main.py` prints dynamic numbers derived directly from the API for the NIFTY 50 instead of `24100 / 23900`. 

## Edge Cases
- Make sure to handle possible network unavailability with sufficient error logging.
- Ensure the values accurately reflect the previous day by fetching an array of daily data and popping the most recently finalized candle.
