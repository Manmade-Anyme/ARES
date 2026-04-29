# Architecture Decision Record — TASK-001 Dynamic PDH/PDL Oracle
**Date:** 2026-04-28
**Status:** approved

## Problem Statement 
The system requires real previous day high (PDH) and low (PDL) levels, but the manual configurations inside `main.py` introduce user friction. The objective is to make this operation dynamic and error-free on startup.

## Decision and Rationale
We will build a custom fetch method `fetch_previous_day_ohlc` in the `PriceFetcher` class located in `fetchers/price_fetcher.py`. Instead of enforcing `dhanhq.historical_daily_data` which we found sporadically unreliable and prone to timeout behavior for INDEX targets, we will utilize `httpx.AsyncClient` alongside Yahoo Finance API (`query2.finance.yahoo.com`).

**Rationale:**
1. Zero Dependency Issue: `httpx` is already documented in `requirements.txt`.
2. Async Output: Perfectly meshes with `PriceFetcher`'s other native routines avoiding threading wrapper latency.
3. Weekend Proof: Yahoo Finance only returns valid trading days eliminating datetime gymnastics for Indian holidays and weekend days.

## Alternatives Considered
- `dhanhq.historical_daily_data` — Discarded since earlier synchronous loop execution returned hanging traces leading to initialization blockage. 
- Setting `.env` cron scripts — Manual architecture complexity not robust for containerization (like Fly.io).

## Component Boundaries 
- `PriceFetcher` (fetchers/price.py): Gets the `fetch_previous_day_ohlc` method.
- `main.py`: Consumes `PriceFetcher` prior to setting `LevelFetcher`. 

## Output Contracts
`fetch_previous_day_ohlc` will execute an async request and return a `Tuple[float, float]` indicating `(PDH, PDL)`. In the event of a catastrophic failure, it may return standard defaults or raise an exception to be caught in main.
