# Problem Brief — TASK-005
**Date:** 2026-05-06
**Status:** draft

## Problem Statement (Original)
why are we fetch PDH and PDL data from yfinance? please update the fetching logic which should be done from dhan api itself please remove yfinance functionality. refer this doc https://dhanhq.co/docs/v2/market-quote/

## Problem Statement (Simplified)
Migrate the fetching of Previous Day High (PDH) and Previous Day Low (PDL) from Yahoo Finance to the Dhan API. Remove all dependencies and logic related to Yahoo Finance.

## What We Know
- Current logic in `fetchers/price_fetcher.py` uses `httpx` to fetch data from `query2.finance.yahoo.com`.
- The user wants to use Dhan's Market Quote API.
- Asset: Nifty 50 (Security ID: 13, Exchange: IDX_I).
- The goal is to get the OHLC of the previous trading day.

## What We Don't Know (Assumptions to Validate)
- Does the Dhan Market Quote API (`/marketquote`) provide the *previous* day's High/Low directly, or do we need to fetch a 1-day historical candle?
- The documentation link provided points to Market Quote. Usually, this contains `ohlc` for the current day and `prevClose`. If `prevHigh` and `prevLow` are not in the quote, we might need to use `intraday_daily_data` or similar.

## Edge Cases Identified
- Market Holidays: If the previous day was a holiday, we need the last actual trading day.
- API Failures: If Dhan API fails, we need a fallback or clear error reporting (though the user wants to remove yfinance entirely, so no yfinance fallback).

## Clarifying Questions (max 3)
1. Does the Dhan API `quote` response contain previous day's High and Low, or should we use the daily historical data endpoint?
2. Should we keep `httpx` for Discord alerts, or was it only for Yahoo Finance? (Likely keep for Discord).

## Recommended Next Step
→ Architect Agent to define the new API call structure and update the PriceFetcher interface.
