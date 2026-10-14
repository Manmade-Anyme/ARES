# TASK-212 QA

## Original PR #130 validation (superseded scope)

Verdict: PASS for the final minimal configuration diff.

- Current-main baseline: 1,254 tests and 45 subtests passed.
- Final full suite: 1,258 tests and 45 subtests passed; 12 pre-existing warnings.
- Focused config/levels/gate suite: 50 passed.
- Related engine/config coverage suite: 113 passed.
- Combined engine/config line coverage: 209/223 (93.72%). Branch coverage: 54/66 (81.82%). Meets pipeline thresholds of 80% line / 70% branch; uncovered paths are outside this change.
- Regression coverage includes exact 0.60 acceptance, below-threshold rejection, both directions, both profiles, global fallback and unchanged non-exhaustion settings.

Commands: `python3 -m pytest tests/ --import-mode=importlib -q`; focused tests in `test_config_profiles.py`, `test_per_type_levels.py`, `test_engine_efficiency_gates.py`. Coverage used related engine/config suites with `--cov=config_profiles --cov=engine --cov-branch`.

Local Python 3.14.6 requires importlib mode because an installed package shadows the repository's `tests` namespace under default import mode. The same workaround was used for baseline and final checks. No repository workaround was introduced; hosted CI runs the project's standard Python 3.10 configuration.

## 2026-10-14 — Scope Amendment QA (SL-Only Verification)

Verdict: PASS for SL-only configuration with R:R override removed.
- Focused suite (`tests/unit/test_config_profiles.py`, `tests/unit/test_per_type_levels.py`, `tests/unit/test_engine_efficiency_gates.py`): 50 passed, 4 subtests passed.
- Full test suite (`python3 -m pytest tests/ --import-mode=importlib -q --cov=config_profiles --cov=engine --cov-branch --cov-report=term`): 1,258 passed, 12 pre-existing warnings, 49 subtests passed.
- Verified exact acceptance:
  - `git diff 5024f58 -- engine.py` is empty (exact pre-PR130 restore).
  - `config_profiles.py` diff against 5024f58 only modifies exhaustion stop from 10 to 30 (`SetupLevels(30.0, 18.0, 40.0)`).
  - Exhaustion signals rejected by the global 1.0 gate in both directions and profiles while preserving SL30 geometry.
  - Other configured setups (`OI_WALL_REJECTION`, `TREND_CONTINUATION`, `FAILED_BREAKOUT`) remain valid and pass the R:R gate.

- Final combined config/engine coverage: 216/220 lines (98.18%), 61/66 branches (92.42%).
- Independent final diff review: approved with no blocking findings; duplicate changelog entry corrected.
