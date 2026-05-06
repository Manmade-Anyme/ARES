# ADR: TASK-005 Migrate PDH/PDL Fetching to Dhan API

**Date:** 2026-05-06
**Status:** draft

## Problem Statement
The current system fetches Previous Day High (PDH) and Previous Day Low (PDL) from Yahoo Finance using `httpx`. The user wants to consolidate all data fetching to the Dhan API to reduce dependencies and improve reliability.

## Decision
We will replace the Yahoo Finance fetching logic in `PriceFetcher` with a call to the Dhan API's `historical_daily_data` endpoint.

## Rationale
- **Consolidation**: Using a single API (Dhan) simplifies the architecture and authentication.
- **Reliability**: Broker-provided historical data is generally more accurate for trading levels than free public sources like Yahoo Finance.
- **Library Reuse**: The `dhanhq` library already supports `historical_daily_data`, which returns the necessary OHLC values for previous trading days.

## Implementation Details
1.  Modify `fetchers/price_fetcher.py`:
    *   Remove `import httpx`.
    *   Update `fetch_previous_day_ohlc` to use `self.dhan.historical_daily_data`.
    *   Fetch the last few days of daily data and take the most recent completed candle (excluding today if it appears).
2.  Update `config.py`:
    *   Optionally remove `yahoo_symbol` if no longer used, or keep it only for banner display.
3.  Dependency Cleanup:
    *   If `httpx` is not used anywhere else (except Discord), ensure it remains in `requirements.txt` but remove its usage from `price_fetcher.py`.

## Alternatives Considered
- **Dhan Market Quote API**: While mentioned by the user, the `quote_data` method in the `dhanhq` library is more complex to parse and might only provide current day OHLC. `historical_daily_data` is the standard for historical levels.

## Definition of Done
- `PriceFetcher.fetch_previous_day_ohlc` returns correct PDH/PDL from Dhan.
- No calls are made to Yahoo Finance.
- Startup banner correctly displays PDH/PDL fetched from Dhan.
