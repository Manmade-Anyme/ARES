# MANM-154 Audit and resolve feature-versioning and missing-data inconsistency
**Date:** 2026-09-12
**Status:** ready

## Goal
Audit the missing features in `ml_collection`, determine root causes (schema versions vs bugs vs API failures), enforce correct NULL/NaN storage, add feature version metadata to future records, and produce an analysis/recommendation report for handling the historical missing data.

## Inputs
- Issue MANM-154: "Audit and resolve feature-versioning and missing-data inconsistency in ml_collection"
- Active codebase `ARES` (fetch from github repo)

## Tools / Scripts to Use
- Standard git/codebase exploration tools
- Python/Pandas or similar for data audits (if applicable)

## Expected Output
- ADR detailing the architectural decisions on how to version schemas and manage missing data properly.
- Missingness audit report broken down by date, market session, and feature version.
- Code updates to enforce correct `NULL`/`NaN` storage instead of fabricated zeros (e.g. fixing bug MANM-49) and storing `feature_version` on all new rows.

## Acceptance Criteria
- Feature/schema version metadata (`feature_version`) is added to all future `ml_collection` records.
- Missingness audit report is generated.
- Missing values are stored as `NULL`/`NaN` rather than fabricated zeros or misleading sentinel values.
- Recommendation produced on whether historical rows should be excluded, imputed, or retained with explicit indicators.

## Edge Cases
- Watch out for zero-feature injection bug (MANM-49) where missing data was mapped to zero, which can skew ML models.
