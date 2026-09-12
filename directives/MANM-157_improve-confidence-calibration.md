# MANM-157 Improve confidence calibration and reliability across signal tiers
**Date:** 2026-09-12
**Status:** ready

## Goal
Improve confidence calibration and reliability across signal tiers to ensure HIGH confidence signals statistically outperform MEDIUM confidence signals in win rate and expectancy.

## Inputs
- ML trade history/data showing current inverse calibration.
- Project code (specifically confidence tier thresholding criteria and detector scoring heuristics).

## Tools / Scripts to Use
- Standard Multica and Python ML tools/scripts available in the repository.

## Expected Output
- ADR detailing the architectural and threshold changes for confidence scoring.
- Implemented calibration evaluation metrics (reliability diagrams, Brier score decomposition, ECE).
- Strict policy enforcement code ensuring HIGH confidence only when historically validated.
- Stratified calibration evaluation results.

## Acceptance Criteria
- Statistical significance testing is conducted and proves whether confidence tiers separate outcome distributions.
- Calibration evaluation metrics (Brier, ECE) are implemented and documented.
- Thresholding criteria reviewed and updated.
- Strict policy code is enforced: HIGH confidence signals must have statistically superior win rate/expectancy out-of-sample.
- Evaluation is stratified by setup type, trade direction (bullish vs bearish), market volatility regime, and time of day.

## Edge Cases
- Handling edge cases where there is insufficient historical data to form a statistically significant confidence tier.
- Regimes where volatility drastically shifts, impacting calibration.
