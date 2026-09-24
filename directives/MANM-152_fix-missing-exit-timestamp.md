# MANM-152 Fix missing exit_timestamp for closed trade and enforce timestamp validation
**Date:** 2026-09-12
**Status:** ready

## Goal
Locate the missing `exit_timestamp` for the closed trade, update the record or flag it if unrecoverable, and add strict validation to prevent trades from closing without an exit timestamp.

## Inputs
- Issue description from MANM-152
- Source code in the repository (specifically `position_manager.py` and db layer).
- Database query tools to inspect `trade_analytics` and `active_trades`.
- Logs (application, execution, Discord notifications).

## Tools / Scripts to Use
- SQL / Database access scripts to find the trade without `exit_timestamp`.
- grep/search to find where trades are marked closed.

## Expected Output
- ADR (by Architect) detailing how we will handle missing timestamps and enforce them.
- Updated database or explicitly flagged record.
- Code changes in `position_manager.py` (and potentially DB routines) enforcing the `exit_timestamp`.
- Regression test asserting `exit_timestamp` is required on trade finalization.

## Acceptance Criteria
- Trade record located and timestamp recovered, OR explicitly flagged and excluded from duration/time-based metrics.
- Strict validation added preventing a trade from transitioning to closed without `exit_timestamp`.
- Regression test passes.

## Edge Cases
- Timestamps cannot be found anywhere in logs.
- Multiple missing timestamps (if the bug affected more than one trade).
