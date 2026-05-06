# TASK-007 Trade Analytics Persistence
**Date:** 2026-05-06
**Status:** ready

## Goal
Implement a separate, highly detailed persistence layer for trade analytics. This feature must store every trade signal and its final result with comprehensive market context (OI, price action, etc.) for monthly analysis and future ML training.

## Inputs
- `AresSignal` objects from the engine.
- `ATMStrikes` and `OptionChain` data from `OIFetcher`.
- Live spot price updates.
- Trade execution/update events from `PositionManager`.

## Tools / Scripts to Use
- Supabase (PostgreSQL) for storage.
- Python `supabase` client.
- Existing fetchers for market context.

## Expected Output
1.  **Supabase Table:** A new table `trade_analytics` (or similar) with a comprehensive schema.
2.  **Persistence Logic:** A new class `AnalyticsLogger` (or extension of `Storage`) that captures snapshots at entry and updates at exit.
3.  **Integration:** Hook into `PositionManager` to trigger analytics logging without affecting existing session management logic.

## Acceptance Criteria
- [x] New Supabase table created with fields for: entry/exit price, timestamp, OI data (PCR, ATM OI), market condition (reasons), and final P&L.
- [x] Trades are recorded in this table even if the system restarts (must handle the transition from OPEN to CLOSED).
- [x] No changes to the existing `active_trades` or `ares_signals` logic.
- [x] Documentation updated to reflect the new analytics schema.

## Edge Cases
- **Missing OI Data:** Handle cases where OI data fetch fails gracefully (store Null or log error).
- **System Crash:** Ensure partial trade data is preserved and updated when the trade eventually closes.
- **Duplicate Signals:** Ensure signals that don't result in trades are either ignored or marked as "NOT_TAKEN".

## Definition of Done
- Schema deployed to Supabase.
- Implementation code merged and tested.
- Sample data verified in Supabase.
