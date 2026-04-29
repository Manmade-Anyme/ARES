# Problem Brief — TASK-001
**Date:** 2026-04-28
**Status:** approved

## Problem Statement (Original)
lets make Make PDH/PDL Dynamic

## Problem Statement (Simplified)
The system currently uses hardcoded Previous Day High (PDH) and Previous Day Low (PDL) values to identify significant support and resistance bounds. This must be automated to fetch dynamic yesterday values from a reliable data oracle upon startup. 

## What We Know
- The values are currently hardcoded in `main.py` explicitly as `pdh, pdl = 24100.0, 23900.0`.
- They are injected natively to `LevelFetcher` using `level_fetcher.set_previous_day_levels()`.
- Dynamic levels from OI Options walls are working efficiently.

## What We Don't Know (Assumptions to Validate)
- Whether `dhanhq.historical_daily_data` operates without timeout on the target platform (it timed out on preliminary tests).

## Edge Cases Identified
- **Data Source Unavailability:** If the oracle times out, the `main.py` script shouldn't completely crash; it could default to last cached or basic fallback.
- **Weekend / Holiday Filtering:** Finding the true "previous trading day" means skipping weekends. Yahoo Finance does this cleanly on API side returning only valid candle series.

## Recommended Next Step
→ Product Manager Agent to finalize the directive allowing alternative reliable endpoints like Yahoo Finance if DhanHQ API exhibits instability for index daily historicals.
