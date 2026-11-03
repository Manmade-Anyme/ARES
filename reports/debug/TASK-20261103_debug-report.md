# Debug Report — TASK-20261103 Tuesday Expiry Threshold Audit

**Verdict:** INCONCLUSIVE — OI-wall threshold sensitivity is demonstrated in the replay subset; a full-engine root cause is not proven.

## Scope and reproduction evidence

- Replayed Dhan minute data for 2026-11-03 through approximately 12:05 IST: 168 index minutes, 167 option-chain minutes, ATM−2 through ATM+2, with all 10/10 option-history requests successful and no duplicate-OI conflicts.
- Supabase contained 156 snapshots through 12:04:49 IST and no signal under the then-active profile.
- Replay covered `OIWallDetector`/`OIWallEntryFilter` and the exhaustion volume/body gate. It did not run the full ARES engine or establish executed trades or P&L.

## Ranked hypotheses and findings

1. **OI growth threshold suppresses expiry wall qualifications.** At current settings (10M minimum OI, 15% growth, original distances), replay produced 3 wall-active minutes, 2 candidate episodes, and 0 qualified entries. Changing only growth to 5% produced 18 active minutes, 7 episodes, and still 0 qualified entries. At 2.5% growth with the 10M floor and original distances, 1 entry qualified; proposed geometry produced 2. This supports a combined threshold/geometry sensitivity, not a 5%-only fix.
2. **The 10M OI floor is the primary cause.** With the proposed OI subset, changing the floor from 10M to 6M produced the same 2 qualified entries in this sample. It added candidate episodes but no additional qualified entry. Sample p85 was about 3.88M CE and 9.93M PE, so 6M is not p85 for both sides.
3. **Original wall distances are too narrow for these observations.** With 2.5% growth, widening approach/test/excursion/retest distances changed the qualified count from 1 to 2 in the detector/filter replay. Full-engine effects remain unmeasured.
4. **The exhaustion volume threshold alone explains zero expiry signals.** Lowering the multiplier from 3.5× to 2.5× still produced zero raw exhaustion candidates with the unchanged 0.30 body-ratio gate. This threshold change alone did not produce a candidate in the partial session.
5. **Other detectors or engine gates suppress the resulting candidates.** Breakout/continuation settings, detector priority, cooldown, and R:R interactions were not replayed through the full engine; this hypothesis remains untested.

## Change under review

The feature branch applies the approved 16 expiry-profile values. Detector logic, the non-expiry profile, expiry candle-body ratio, continuation enablement, and per-setup SL/T1/T2 are unchanged. Values and historical evidence are recorded in the Ares Build Log and PR #154.

## Verification and limits

- Focused profile tests passed: `python -m pytest tests/unit/test_config_profiles.py -q` — 18 passed on commit `82f0ded`.
- `git diff --check` was clean on that commit.
- No full-engine Tuesday replay, full test-suite result, profitability result, or broker-fill result is available in this report.

The replay supports a sensitivity trial. It does not prove the whole configuration bundle improves signals, win rate, or P&L.
