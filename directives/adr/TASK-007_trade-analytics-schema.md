# ADR-007: Trade Analytics Persistence Schema
**Date:** 2026-05-06
**Status:** Approved

## Problem Statement
The user requires a permanent, detailed record of every trade and its outcome for monthly analysis and machine learning. The current implementation in `active_trades` is ephemeral (purged daily) and lacks deep market context (OI data, reasons).

## Decision
We will implement a new table `trade_analytics` in Supabase and a corresponding `AnalyticsLogger` class in `storage.py`. This will be independent of the existing `PositionManager` session state to ensure stability and separation of concerns.

## Schema Design
```sql
CREATE TABLE trade_analytics (
  id uuid PRIMARY KEY,
  signal_id bigint, -- Optional link to ares_signals
  setup_type text NOT NULL,
  direction text NOT NULL,
  
  -- Price & Time
  entry_timestamp timestamptz NOT NULL,
  exit_timestamp timestamptz,
  entry_price numeric NOT NULL,
  exit_price numeric,
  pnl_points numeric,
  
  -- Outcome
  result_state text DEFAULT 'OPEN', -- OPEN, T1_HIT, T2_HIT, STOPPED_OUT, EXPIRED
  
  -- Deep Context (JSONB for ML flexibility)
  market_context jsonb, -- { "reasons": [...], "spot_at_signal": 24500, "confidence": "HIGH" }
  oi_data jsonb,        -- { "pcr": 0.8, "atm_ce_oi": 1200000, "atm_pe_oi": 1500000, "oi_change_pct": 5.2 }
  
  created_at timestamptz DEFAULT now()
);

-- Index for temporal analysis
CREATE INDEX idx_trade_analytics_entry ON trade_analytics (entry_timestamp DESC);
```

## Implementation Plan

### 1. `storage.py` Updates
- Add `AnalyticsLogger` class.
- Method `log_entry(trade_id, signal, atm_strikes)`: Creates the initial row in `trade_analytics`.
- Method `log_exit(trade_id, exit_price, final_state)`: Updates the row with exit details and calculates P&L.

### 2. `position_manager.py` Integration
- The `PositionManager` will hold an instance of `AnalyticsLogger`.
- In `add_trade()`: Call `logger.log_entry()`.
- In `update_trades()`: When a trade hits SL or Target, call `logger.log_exit()`.

### 3. Data Capture
- **OI Data:** Capture `ATMStrikes` data at the moment of entry.
- **Market Context:** Capture `AresSignal.reasons` and other metadata.

## Alternatives Considered
- **Modify `active_trades`**: Rejected. The user explicitly requested a separate feature to avoid breaking current functionality.
- **Local CSV/JSON**: Rejected. Supabase is already used and provides better accessibility for ML tools and remote analysis.

## Definition of Done
- SQL schema applied to Supabase.
- `AnalyticsLogger` implemented in `storage.py`.
- Integration hooks added to `position_manager.py`.
- Verified that `active_trades` still functions as expected (ephemeral session state).
