# Sprint Plan
**Date:** 2026-09-12

## Objective
Refactor the ML training pipeline for ARES to decouple self-labeled and realized-outcome datasets, and implement robust walk-forward validation (MANM-155).

## Tasks for Today
1. **[MANM-155]** Architect Agent to write an ADR and architecture docs for the decoupled ML pipelines and walk-forward cross-validation.
2. **[MANM-155]** Code Generator Agent to implement the refactored `ml_signal/train_offline.py` based on the ADR.
3. **[MANM-155]** QA Agent to verify data leakage guards, test overlap, and pipeline robustness.
