# Problem Brief — TASK-007
**Date:** 2026-05-06
**Status:** approved

## Problem Statement (Original)
Yes i want to create a separate table where it storesa all the data of trades given and their result, i want it to be as detailed as possible, i ll be using it at the end of the month to analyse the data. A separate table and functionaliyu will be good as i dont want to make any changes in the current implementation. make it like a separate feature. 
i should store all the data like entry exit, market condition, oi data(not all but enough to get an idea of what happened and make sense of why the trade was taken) and any required data for analysis. Ill be creating a ML for this data stored in Supabase to analyse and pick some pattern. 

## Problem Statement (Simplified)
Implement a permanent, detailed trade analytics persistence layer in Supabase that captures market context (OI, price, reasons) at entry and final outcomes at exit, independent of the current session-based `active_trades` logic.

## What We Know
- The user wants a separate table (`trade_analytics`) to avoid interfering with current logic.
- Data must be detailed enough for ML analysis (entry, exit, OI, market conditions).
- Current `PositionManager` handles live trades but purges data daily.
- `OIFetcher` already retrieves the necessary OI data during the cycle.

## What We Don't Know (Assumptions to Validate)
- **Market Condition specifics:** We will capture `AresSignal.reasons` as the primary market context, along with spot price and confidence.
- **OI Data specifics:** We will capture ATM PCR (implied from ATM CE/PE OI), and ATM OI change percentages.

## Edge Cases Identified
- **Partial Data:** A trade might be opened but the system might crash before it's closed. The logic must handle updating existing rows when the trade eventually closes.
- **Dhan API Failures:** If OI data cannot be fetched at the moment of entry, the row should still be created with Nulls for OI fields rather than failing the trade.

## Contradictions Found
- None. The request for a "separate feature" simplifies the implementation by avoiding refactoring of existing ephemeral tables.

## Recommended Next Step
→ Code Generator Agent: Implement `AnalyticsLogger` in `storage.py` and integrate it into `PositionManager` as per ADR-007.
