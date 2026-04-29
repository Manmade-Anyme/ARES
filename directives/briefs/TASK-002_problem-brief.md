# Problem Brief — TASK-002
**Date:** 2026-04-28
**Status:** approved

## Problem Statement (Original)
first make it Security id and exchange in env. secondly why did it fetch pdl and pdh for nifty when if=ve added for goldm

## Problem Statement (Simplified)
The system was explicitly configured for NIFTY 50 via specific `.env` variables (`NIFTY_SECURITY_ID`, `NIFTY_EXCHANGE`) and a hardcoded Yahoo Finance ticker (`^NSEI`). When the user swapped the `.env` variables to `GOLDM` credentials, the oracle continued extracting boundary data for Nifty, creating a state inconsistency based on asset assumptions.

## What We Know
- The `.env` template assumes NIFTY names.
- `config.py` encodes NIFTY properties specifically (`nifty_security_id`).
- `price_fetcher.py` currently hardcodes the string `^NSEI`.

## What We Don't Know (Assumptions to Validate)
- Whether the provided `YAHOO_SYMBOL` maps directly to exact MCX timings on Yahoo Finance (Yahoo Finance commodity charts might differ). User must handle the symbol accuracy.

## Edge Cases Identified
- An invalid `YAHOO_SYMBOL` will trigger the explicit fallback mechanism implemented in TASK-001 (printing a warning and applying defaults).

## Recommended Next Step
→ Product Manager Agent should formally mandate replacing Nifty-specific variable names with general definitions.
