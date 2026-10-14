# TASK-212 QA

Verdict: PASS for the final minimal configuration diff.

- Current-main baseline: 1,254 tests and 45 subtests passed.
- Final full suite: 1,258 tests and 45 subtests passed; 12 pre-existing warnings.
- Focused config/levels/gate suite: 50 passed.
- Related engine/config coverage suite: 113 passed.
- Combined engine/config line coverage: 209/223 (93.72%). Branch coverage: 54/66 (81.82%). Meets pipeline thresholds of 80% line / 70% branch; uncovered paths are outside this change.
- Regression coverage includes exact 0.60 acceptance, below-threshold rejection, both directions, both profiles, global fallback and unchanged non-exhaustion settings.

Commands: `python3 -m pytest tests/ --import-mode=importlib -q`; focused tests in `test_config_profiles.py`, `test_per_type_levels.py`, `test_engine_efficiency_gates.py`. Coverage used related engine/config suites with `--cov=config_profiles --cov=engine --cov-branch`.

Local Python 3.14.6 requires importlib mode because an installed package shadows the repository's `tests` namespace under default import mode. The same workaround was used for baseline and final checks. No repository workaround was introduced; hosted CI runs the project's standard Python 3.10 configuration.
