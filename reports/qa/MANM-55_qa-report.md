# MANM-55 QA Gate Report — Final

## Verdict: PASS

The final MANM-55 implementation satisfies the strict differential QA gate.

## Scope and workflow guard

| Check | Result |
|---|---|
| Branch | `feature/ML-SHAP-training-report` |
| `git diff --check` | PASS — no whitespace errors |
| Product diff | `CHANGELOG.md`, `ml_signal/README.md`, `ml_signal/requirements.txt`, `ml_signal/train_offline.py`, `tests/unit/test_task183_ml_offline.py` |
| QA artifact | This report only |
| Workflow diff | PASS — no `.github` workflow change |
| Workflow identity | PASS — `.github/workflows/ml_training.yml` hashes to required blob `f34e488cf0d52cb3dcb4ca2a07ecaaffeeaccfcc` |
| Live/runtime paths | PASS — no diff to `trainer.py`, predictor, collector, engine, or runtime integration |

## Test results

| Command | Result |
|---|---|
| `python -m pytest tests/unit/test_task183_ml_offline.py tests/unit/test_task205_ml_workflow.py -q` | PASS — **36 passed in 3.34s** |
| `python -m pytest tests/unit/test_task183_ml_offline.py -q` | PASS — **34 passed in 1.97s** (also used by target tracer) |
| `python -m pytest -q` | PASS — **393 passed, 1 warning, 8 subtests passed in 33.08s** |

The sole full-suite warning is existing XGBoost guidance when an older serialized model is unpickled in `test_task199_audit_fixes.py`; it is outside this diff.

## Exact changed-code coverage

`pytest-cov` was attempted with `--cov=ml_signal.train_offline --cov-branch` but cannot collect on local Python 3.14.6: NumPy aborts collection with `ImportError: cannot load module more than once per process`. No packages were installed.

The established target-only fallback was used instead: an in-memory `sys.settrace` line/arc tracer ran the focused TASK-183 suite, while Coverage.py's static parser identified changed executable statements and changed decision arcs from the zero-context Git diff. Return arcs were included.

| Metric | Exact result | Required |
|---|---:|---:|
| Changed executable statements | **121 / 121 = 100.00%** | 100% |
| Changed branch arcs | **34 / 34 = 100.00%** | 100% |

Uncovered changed statements: none.
Uncovered changed branch arcs: none.

## Functional and assertion-quality checks

| Requirement | Result |
|---|---|
| Preferred external contract | PASS — a real `run_training()` test asserts the actual chronological held-out `X_test`, `tree_limit == best_iteration + 1`, and `check_additivity is True` passed to `TreeExplainer` |
| Import-failure distinction | PASS — only `ModuleNotFoundError` whose `name` is exactly `shap` skips; nested-module and generic import failures take native exact TreeSHAP fallback |
| BaseException control | PASS — relevant optional/fallback/plot handlers catch `Exception`, not `BaseException`; interrupts and termination signals are not swallowed |
| Native fallback validation | PASS — exact XGBoost `pred_contribs` validates shape, finiteness, bias-inclusive raw-margin additivity, and fitted best-iteration range |
| Total fallback failure | PASS — regular native failure records `shap_status=failed`, preserves model/report persistence, and remains nonfatal |
| Row-count truthfulness | PASS — initialized/skip/failure count is `0`; only validated success sets it to `len(X_test)` |
| SHAP output schema | PASS — stable status/backend/output-unit/top-15 fields; ranking is aligned, nonnegative, and descending |
| Plot behavior | PASS — headless 150-DPI PNG, figure close, optional no-plot/no-SHAP, and plot failure nonfatal paths are asserted |
| Documentation | PASS — README/changelog correctly state offline-only scope, held-out split, best iteration, raw-margin/log-odds units, compatibility fallback, report/PNG artifacts, and unchanged live inference |

## Offline synthetic artifact smoke

No live data, network access, package installation, Supabase client, or workflow execution was used. A deterministic 64-row synthetic frame trained a real XGBoost model with a forced optional-SHAP compatibility error, exercising the production native exact-contribution fallback.

| Artifact/check | Result |
|---|---|
| Reloaded model | PASS — 20,301 B; produced two predictions |
| JSON report | PASS — 1,264 B; `model_version=vsmoke`, `shap_status=computed`, `shap_backend=xgboost_pred_contribs` |
| Row count/additivity | PASS — `13 == n_test`; raw-margin additivity true |
| PNG | PASS — 27,444 B; readable `PNG`, `1481×883`, RGBA; plot marked saved |

## Risks

- The local Python 3.14 environment cannot run pytest-cov against NumPy, so exact diff coverage was measured with the documented static-parser plus dynamic target-tracer fallback.
- The optional real SHAP package's successful external runtime path is represented by the strict boundary-contract test; the real XGBoost native fallback and artifact path were independently exercised.
- The pre-existing serialized-model XGBoost warning remains.

## Recommendation

QA approved. MANM-55 meets the requested implementation, regression, artifact, scope, workflow, and 100% changed statement/branch coverage gates.

## MANM-55 Reviewer-Fix Verification Addendum — 2026-09-01

### QA checklist

- [x] A stale regular SHAP PNG is removed when SHAP was not computed.
- [x] An absent path remains nonfatal and reports `not_saved_no_shap`.
- [x] A symlink is preserved with its target intact; cleanup applies only to a regular, non-symlink file.
- [x] A removal `OSError` is nonfatal and reports `stale_cleanup_failed` plus `OSError`.
- [x] `run_training()` still returns a model and preserves its normal metrics/report behavior.
- [x] No ML training deployment workflow change is present.

### Commands and results

| Command | Result |
|---|---|
| Three cleanup tests (regular file, symlink, removal error) | PASS — **3 passed in 2.15s** |
| `python -m pytest tests/unit/test_task183_ml_offline.py -q` | PASS — **37 passed in 2.93s** |
| `python -m pytest tests/unit/test_task183_ml_offline.py tests/unit/test_task205_ml_workflow.py -q` | PASS — **39 passed in 2.84s** |
| `python -m pytest -q` | PASS — **396 passed, 1 existing XGBoost serialization warning, 8 subtests passed in 33.42s** |

The tests exercise observable `run_training()` outcomes with real temporary files and a real symlink; mocks are limited to the optional SHAP boundary and the removal failure boundary. They do not assert internal call counts.

### Differential coverage

`pytest-cov` remains unavailable in this local Python 3.14.6 environment: collection aborts before tests with NumPy's `ImportError: cannot load module more than once per process`. No package was installed or changed.

The reproducible fallback traced only `_save_shap_plot` while executing the four behavioral paths (absent, regular, symlink, removal error), then used Coverage.py 7.14.2's `PythonParser` to calculate the static arcs for the changed cleanup block.

| Metric | Result |
|---|---:|
| Changed executable statements (`train_offline.py:245-251`) | **7 / 7 = 100.00%** |
| Changed outgoing static arcs | **8 / 8 = 100.00%** |
| Uncovered changed statements/arcs | **None** |

### Scope guard and verdict

- `git diff --check`: PASS.
- `.github/workflows/ml_training.yml`: no diff; `HEAD` and `origin/main` both resolve to blob `f34e488cf0d52cb3dcb4ca2a07ecaaffeeaccfcc`.
- Reviewer-fix production scope is limited to the stale-artifact guard in `ml_signal/train_offline.py`; the associated tests are in `tests/unit/test_task183_ml_offline.py`.

**Verdict: PASS.** The reviewer’s stale-artifact concern is addressed without a regression or workflow change.
