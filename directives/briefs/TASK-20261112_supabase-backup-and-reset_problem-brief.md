# Problem Brief — TASK-20261112_supabase-backup-and-reset
**Date:** 2026-11-12
**Status:** approved

## Problem Statement (Original)
> "I want to first save the trades given till yesterday and saved in Supabase. I want to create a backup with CSV or Excel format and store all the data in Supabase for a raise signal in Excel. And then the trades which we have you got from today they will just be there in super base. The reason for doing this is because I want to clear all the old mixed logs and now I want to test how exactly the system is going to work. We have created and updated the system as per our need. The data which is recorded has a lot of mixed values and is making it difficult to analyze the data properly. Backup today's recorded data in Excel format and delete all previous recorded data in Supabase."

*(Speech-to-text normalization: "a raise signal" → `ares_signals`; "super base" → Supabase.)*

## Problem Statement (Simplified)
Export ARES historical Supabase data (through yesterday, `< 2026-11-12 00:00 IST`) AND today's (`2026-11-12 IST`) snapshot to local Excel/CSV backup files, verify 100% row-count parity, and safely delete pre-today (`< 2026-11-12 00:00 IST`) ARES records across all 8 ARES event/log tables so only today's (`2026-11-12`) clean post-update data remains in Supabase.

## What We Know (Verified Live State as of 2026-11-12 11:44 IST)
- **Active Open Trades:** `0` non-closed rows in `active_trades`.
- **Pre-Purge Row Counts (`today` = `>= 2026-11-12T00:00:00+05:30` / `2026-11-11T18:30:00Z`):**
  | Table | Total | Pre-Today (`< 2026-11-12 IST`) | Today (`>= 2026-11-12 IST`) | Role |
  |---|---:|---:|---:|---|
  | `ares_signals` | 402 | 401 | 1 (`#1627`) | Core signals |
  | `active_trades` | 400 | 399 | 1 (`#1627`) | Trade state |
  | `trade_analytics` | 400 | 399 | 1 (`#1627`) | Trade outcomes & option accounting |
  | `ml_collection` | 23,828 | 23,691 | 137+ (live) | 1-min feature & training snapshots (JSONB-heavy) |
  | `ml_predictions` | 92 | 91 | 1 | XGBoost inference audit log |
  | `llm_predictions` | 90 | 89 | 1 | Jev forward predictions |
  | `llm_prediction_jobs` | 92 | 91 | 1 | Jev outbox / Discord delivery state |
  | `oi_watch_predictions` | 9 | 9 | 0 | OI-watch outbox |
  | `llm_consumer_state` | 1 | 1 | 0 | Operational singleton (DO NOT DELETE) |
  | `oi_watch_producer_state` | 1 | — | — | Operational singleton (DO NOT DELETE) |
  | `latest_quotes` | 1 | — | — | Live quote cache (DO NOT DELETE) |
  | `api_keys` | — | — | — | Dhan credentials (DO NOT TOUCH) |
- **Shared Database:** Supabase `postgres` DB is shared with Gamma Blaster (`gb_*`), Kronos, Phantom, Sniper, and Order Flow (`schema.sql`).
- **Environment:** `pandas 3.0.3` and `openpyxl` are installed locally.

## Resolved Decisions (Approved by User)
1. **Backup Scope:** Export **both** pre-today (`<= 2026-11-11 IST`) and today's snapshot (`2026-11-12 IST`) before deleting pre-today rows.
2. **Table Scope:** Purge all **8 ARES event/log tables** (`ares_signals`, `active_trades`, `trade_analytics`, `ml_collection`, `ml_predictions`, `llm_predictions`, `llm_prediction_jobs`, `oi_watch_predictions`) for `< 2026-11-12 00:00 IST`. Leave `llm_consumer_state`, `oi_watch_producer_state`, `latest_quotes`, `api_keys`, and all other projects untouched.
3. **Deliverable & Format:** One-off operational backup + purge (no code PR). `.xlsx` workbooks for signals/trades/predictions (plus lossless `.csv` fallback when `trade_analytics.market_context` exceeds Excel's 32,767-char cell limit) and `.csv` for `ml_collection`.
