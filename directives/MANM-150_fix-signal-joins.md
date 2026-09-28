# MANM-150 Fix broken signal-to-trade joins
**Date:** 2026-09-12
**Status:** draft

## Goal
Fix broken signal-to-trade joins so that `trade_analytics`, `active_trades`, and `ml_collection` tables all correctly reference a canonical UUID. New schemas use `ares_signals.id`; existing deployments use the additive `ares_signals.signal_uuid` bridge until a separately reviewed primary-key cutover.

## Inputs
- `storage.py`
- `position_manager.py`
- `ml_signal/collector.py`
- `models.py` (AresSignal model)

## Tools / Scripts to Use
- `schema.sql` (to review database schema)
- `pytest` for running integration tests

## Expected Output
- Architectural Decision Record (ADR) capturing the canonical key decisions and transition strategy.
- Code changes in data models and insertion paths enforcing the canonical UUID contract without writing UUIDs into an existing bigint `ares_signals.id` column.
- A safe read-only validation query and a documented historical backfill plan.
- Integration tests ensuring correct signal joins.

## Acceptance Criteria
- New schemas use `trade_analytics.signal_id` and `ml_collection.signal_id` as UUID foreign keys to `ares_signals.id`; existing schemas use UUID bridge columns referencing `ares_signals.signal_uuid` until primary-key cutover.
- The display IDs (4-digit format) are preserved in a dedicated display column if needed for UX/alerts.
- Tests verify signal -> active trade -> trade analytics -> ML collection relationships.
- Historical data is left untouched until backfill is fully planned and unambiguous.

## Edge Cases
- Distinguishing between older existing database schemas/values versus new runtime behaviors.
- Handling ambiguous matches in historical rows safely.
