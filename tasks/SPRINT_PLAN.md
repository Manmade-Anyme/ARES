# Sprint Plan
**Date:** 2026-09-12

## Objective
Audit and resolve feature-versioning and missing-data inconsistency in `ml_collection` (MANM-154).

## Tasks for MANM-154
1. Write the initial intent directive (Done)
2. Architect to review the issue, explore codebase, and produce an ADR (Done: ADR-154)
3. Code Generator to implement changes based on ADR:
   - Migration SQL for `feature_version` column and default tagging
   - Update `collector.py` and `signal_consumer.py` to persist `feature_version`
   - Enforce `NULL`/`NaN` storage invariants (no zero/sentinel fallbacks)
   - Dataset loading updates in `dataset.py` & dual-track training in `train_offline.py`
   - Produce missingness audit report script/artifact
4. QA to review and validate test coverage and invariants.

