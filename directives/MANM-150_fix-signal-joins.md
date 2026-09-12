# MANM-150 Fix broken signal-to-trade joins
**Date:** 2026-09-12
**Status:** draft

## Goal
Fix broken signal-to-trade joins so that `trade_analytics`, `active_trades`, and `ml_collection` tables all correctly reference `ares_signals.id` as their canonical join key instead of 4-digit display IDs.

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
- Code changes in data models and insertion paths enforcing the use of the canonical `ares_signals.id` UUID primary key.
- A safe read-only validation query and a documented historical backfill plan.
- Integration tests ensuring correct signal joins.

## Acceptance Criteria
- `trade_analytics.signal_id` and `ml_collection.signal_id` store actual UUID keys corresponding to `ares_signals.id`.
- The display IDs (4-digit format) are preserved in a dedicated display column if needed for UX/alerts.
- Tests verify signal -> active trade -> trade analytics -> ML collection relationships.
- Historical data is left untouched until backfill is fully planned and unambiguous.

## Edge Cases
- Distinguishing between older existing database schemas/values versus new runtime behaviors.
- Handling ambiguous matches in historical rows safely.
