# Architecture Decision Record — TASK-002 Asset Agnostic Engine
**Date:** 2026-04-28
**Status:** approved

## Problem Statement
ARES defaults to querying NIFTY-centric values, preventing proper context shifts mapping to other assets like Commodities (GOLDM).

## Decision and Rationale
We remove the asset-specific naming layer across the configuration boundary (`Pydantic BaseSettings`).
Assets require configuration points at all structural vectors:
1. `SECURITY_ID`: The broker SDK instrument mapping.
2. `EXCHANGE_SEGMENT`: The broker segment mapping.
3. `INSTRUMENT_TYPE`: The broker type mapping (e.g., INDEX vs FUTCOM).
4. `YAHOO_SYMBOL`: The external public oracle matching identifier for boundary values.

All string constants previously coded around NIFTY (like `^NSEI`) will be pulled into the `.env` mapping layer.

## Alternatives Considered
- Writing logic to intelligently detect `exchange_segment` and automatically figure out Yahoo symbols: Discarded because there is no robust systematic 1:1 map between Indian local broker IDs and Yahoo's global symbol dataset. User must define both manually in `.env`.

## Component Boundaries
- `config.py`: Exposes `Settings` schema.
- `.env`: Source of truth.
- `fetchers/price_fetcher.py` & `fetchers/oi_fetcher.py`: Consumers of the dynamic schema.
