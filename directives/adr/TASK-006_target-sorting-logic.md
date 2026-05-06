# ADR: TASK-006 Deterministic Target Sorting Logic

**Date:** 2026-05-07
**Status:** complete

## Problem Statement
The detectors (`FailedBreakoutDetector` and `ExhaustionDetector`) dynamically select Profit Targets (T1 and T2) based on structural levels (PDH/PDL, OI Walls). However, depending on the order in which levels were scanned, T2 could sometimes be closer to the entry price than T1. This inconsistency made it difficult for the `PositionManager` to enforce trailing stop logic (which triggers at T1) and made Discord alerts less intuitive for the trader.

## Decision
1.  **Deterministic Sorting**: Implement a post-process in both detectors to ensure that `Target 1` is always the level closest to the entry price, regardless of the direction (Bullish/Bearish).
2.  **Proximity Filtering**: Filter out structural levels (supports/resistances) that are within **20 points** of the current spot price during target selection. This ensures that selected structural targets offer meaningful profit potential.

## Rationale
- **Predictability**: The `PositionManager` can safely assume T1 is the first milestone.
- **Risk Management**: Trailing the stop loss to entry at T1 is only logical if T1 is the nearest target.
- **Minimum Profitability**: Structural levels that are too close to the entry price (e.g., < 20 points) lead to premature T1 hits and insignificant profit booking. Filtering these ensures the system waits for the next significant level or falls back to more substantial fixed-point targets.
- **UI/UX Consistency**: Discord alerts will always list the targets in chronological order of expected hit.
- **Robustness**: Swapping the targets also requires swapping the corresponding "reason" strings to ensure the alert documentation remains accurate.

## Implementation Details
1.  **Detector Updates**:
    *   In `detectors/breakout.py` and `detectors/exhaustion.py`, add a sorting block after targets are identified.
    *   For **Bearish** trades: If `target_1 < target_2` (T2 is higher/closer to entry), swap them.
    *   For **Bullish** trades: If `target_1 > target_2` (T2 is lower/closer to entry), swap them.
    *   Perform a string replacement in the `reasons` list to keep "Target 1" and "Target 2" labels consistent with the numerical values.
2.  **Test Suite Synchronization**:
    *   Update `tests/unit/test_breakout.py` to reflect the new expected values where T1 is the closer level.

## Alternatives Considered
- **Sorting at Source**: Sorting the levels before selection was considered, but selecting the "next available" level already has some implicit logic. Post-selection sorting is more defensive and easier to reason about.
- **Manual Assignment**: Requiring the detector to "know" which one is closer during selection would have complicated the structural scan loop.

## Definition of Done
- Both detectors guarantee `abs(entry - target_1) < abs(entry - target_2)`.
- Discord alerts display the closer target as "Target 1".
- Unit tests pass with the new deterministic sorting logic.
