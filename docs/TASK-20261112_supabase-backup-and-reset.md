# TASK-20261112: Supabase Historical Backup & Pre-2026-11-12 Clean-Slate Reset

**Date:** 2026-11-12  
**Status:** Executed & Verified  
**Branch:** `feature/TASK-20261112-supabase-backup-and-reset`  
**Cutoff Boundary:** `2026-11-12T00:00:00+05:30` (`2026-11-11T18:30:00Z`)

---

## 1. Why This Was Done
Over recent development iterations (including option reference accounting, option ATR trailing lifecycle, dual-write `market_context.atr_trailing`, Jev forward prediction pipelines, and live option T1/T2 quote capture), historical records accumulated in Supabase across multiple schema versions, calculation revisions, and legacy formats. Because those mixed historical values live alongside current post-update data, querying Supabase for performance analysis and verifying how the updated live system behaves became error-prone.

To establish an uncontaminated baseline for evaluating the updated system starting from `2026-11-12` IST:
1. All historical data through yesterday (`< 2026-11-12 00:00 IST`) — plus a snapshot of today's (`2026-11-12`) initial session records — was exported locally and verified for 100% row-count parity before any deletion.
2. All pre-`2026-11-12 00:00 IST` rows across ARES's 8 event/log tables were purged so live Supabase retains only `2026-11-12` IST onward data.

---

## 2. Backup Outputs & Retention Locations

### Local Repository Backup Directory (Gitignored via `reports/backups/`)
Saved under `reports/backups/2026-11-12/` (excluded from Git via `.gitignore` to avoid committing ~50 MB of raw snapshots and `llm_prediction_jobs.alert_destination` webhook URLs):
- `ares_backup_pre_2026-11-12.xlsx`: Multi-sheet Excel workbook (`summary`, `trades_enriched`, `ares_signals`, `active_trades`, `trade_analytics`, `ml_predictions`, `llm_predictions`, `llm_prediction_jobs`, `oi_watch_predictions`) covering `< 2026-11-12 00:00 IST`.
- `ares_backup_today_2026-11-12.xlsx`: Multi-sheet Excel workbook with the same schema covering the `2026-11-12 IST` session snapshot at export time.
- `trade_analytics_pre_2026-11-12_lossless.csv` (399 rows) & `trade_analytics_today_2026-11-12_lossless.csv` (1 row): Lossless CSV fallbacks generated automatically because `trade_analytics.market_context` JSONB payloads reached `292,193` / `198,518` characters, exceeding Excel's `32,767`-character cell limit.
- `ml_collection_pre_2026-11-12.csv` (23,691 rows) & `ml_collection_today_2026-11-12.csv` (143 rows at snapshot time): Lossless CSV exports for 1-minute feature and training snapshots.

### Obsidian Vault & External Archive
- **Obsidian Vault:** Lossless `.csv` archives copied to `~/Documents/Obsidian/Projects/Ares/Backups/2026-11-12/` and indexed in `~/Documents/Obsidian/Projects/Ares/Backups/Supabase Backups Index.md` (linked from `ARES.md`, `Storage.md`, and `Ares Build Log.md`).
- **Google Drive:** User-managed external backup of the `.xlsx` workbooks and companion CSVs.

---

## 3. Purge Execution & Verification

### Foreign-Key-Safe Deletion Order
Pre-today (`< 2026-11-12T00:00:00+05:30`) records were verified to have `0` non-closed `active_trades`, then fetched by explicit primary key and deleted in FK-safe batches:
1. `llm_prediction_jobs` (`prediction_id` → `llm_predictions.id`)
2. `llm_predictions`
3. `oi_watch_predictions`
4. `ml_predictions`
5. `active_trades` (`signal_uuid` / `signal_id` → `ares_signals`)
6. `trade_analytics` (`signal_uuid` / `signal_id` → `ares_signals`)
7. `ml_collection` (`fk_ml_collection_signal_uuid` → `ares_signals.signal_uuid`)
8. `ares_signals`

### Verified Row Counts (Pre-Purge vs. Post-Purge)

| Table | Deleted (`< 2026-11-12 IST`) | Remaining Pre-Today | Remaining Today (`>= 2026-11-12 IST`) |
|---|---:|---:|---:|
| `ares_signals` | 401 | 0 | 1 (`#1627`) |
| `active_trades` | 399 | 0 | 1 (`#1627`) |
| `trade_analytics` | 399 | 0 | 1 (`#1627`) |
| `ml_collection` | 23,691 | 0 | 145 (live 1m ticks) |
| `ml_predictions` | 91 | 0 | 1 |
| `llm_predictions` | 89 | 0 | 1 |
| `llm_prediction_jobs` | 91 | 0 | 1 |
| `oi_watch_predictions` | 9 | 0 | 0 |

### Untouched Operational & Shared Database Tables
- **ARES operational state & credentials preserved (`total = 1` each):** `llm_consumer_state`, `oi_watch_producer_state`, `latest_quotes`, `api_keys`.
- **Shared Supabase project apps untouched:** Gamma Blaster (`gb_*`), Kronos, Phantom, Sniper, and Order Flow tables were not modified.
