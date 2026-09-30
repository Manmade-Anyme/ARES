# Session Checkpoint
**Date:** 2026-09-30
**Session:** MANM-159 branch sync

## Completed This Session
- MANM-159: Created directive and backlog; completed root cause investigation, statistical bootstrap analysis, outlier sensitivity, setup cross-tabs, and ADR-159 (`directives/adr/MANM-159_directional-performance-asymmetry.md`).
- Integrated current `origin/main` into `feature/MANM-159-performance-asymmetry`, including the MANM-150 through MANM-158 work.
- Reverted the MANM-159 runtime implementation at the user's request; the ADR remains for future evaluation.

## Open Tasks
- MANM-159: Deferred while the current live rules and ML predictions are observed. Keep this branch open for later review.

## Blockers
- None.

## Resume Instructions
Keep current trading rules and this branch. On return, inspect ML prediction quality and directional outcomes, then use out-of-sample replay before considering live rule changes.
