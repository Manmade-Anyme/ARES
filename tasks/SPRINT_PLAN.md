# Sprint Plan
**Date:** 2026-09-25

## Objective
Refactor the ML training pipeline for ARES to decouple self-labeled and realized-outcome datasets, and implement robust walk-forward validation (MANM-155).

## Tasks for MANM-155
1. **[MANM-155]** Architect Agent to write an ADR and architecture docs for the decoupled ML pipelines and walk-forward cross-validation. (Done: `directives/adr/TASK-155_ml-training-refactor.md`)
2. **[MANM-155]** Code Generator Agent to implement the refactored `ml_signal/train_offline.py`, decoupled pipelines, walk-forward cross-validation, and promotion gating based on ADR-155.
3. **[MANM-155]** QA Agent to verify data leakage guards, test overlap, and pipeline robustness.

## Completed Sprints
- **MANM-154**: Feature versioning and missing data remediation (Merged from `main`).
- **TASK-153**: Prediction persistence in `ml_predictions` (Merged from `main`).
- **MANM-150**: Canonical signal-to-trade UUID joins (Merged from `main`).
