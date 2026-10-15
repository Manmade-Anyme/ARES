# TASK-258 Update Jev to Latest Version
**Date:** 2026-10-15
**Status:** ready

## Goal
Upgrade the TypeSafe Jev client dependency (`typesafe-sdk`) to its latest published release (0.7.5) and verify compatibility across all system_one components, inference models, and regression test suites.

## Inputs
- Current dependency configuration: [requirements.txt](file:///Users/manmadeanyme/multica_workspaces_desktop-api.multica.ai/manmade-any-81b609a20e15/manm-258-6d7fdc64385d/workdir/ARES/requirements.txt) (`typesafe-sdk` currently installed at 0.7.2, latest on PyPI is 0.7.5).
- Jev integration code: [system_one/jev.py](file:///Users/manmadeanyme/multica_workspaces_desktop-api.multica.ai/manmade-any-81b609a20e15/manm-258-6d7fdc64385d/workdir/ARES/system_one/jev.py), [system_one/consumer.py](file:///Users/manmadeanyme/multica_workspaces_desktop-api.multica.ai/manmade-any-81b609a20e15/manm-258-6d7fdc64385d/workdir/ARES/system_one/consumer.py), [system_one/watch_prediction.py](file:///Users/manmadeanyme/multica_workspaces_desktop-api.multica.ai/manmade-any-81b609a20e15/manm-258-6d7fdc64385d/workdir/ARES/system_one/watch_prediction.py).
- Existing test suites: `tests/unit/test_task210_system_one.py`, `tests/unit/test_task211_watch_prediction.py`, `tests/unit/test_task211_watch_consumer.py`.

## Tools / Scripts to Use
- `pip install --upgrade typesafe-sdk`
- `pytest tests/unit/test_task210_*.py tests/unit/test_task211_*.py`
- git feature branch workflow: `feature/MANM-258-update-jev-latest`

## Expected Output
- Updated [requirements.txt](file:///Users/manmadeanyme/multica_workspaces_desktop-api.multica.ai/manmade-any-81b609a20e15/manm-258-6d7fdc64385d/workdir/ARES/requirements.txt) pinning/allowing latest `typesafe-sdk>=0.7.5`.
- Updated model version string if required by SDK or Jev model release notes (e.g. `DEFAULT_MODEL` in [system_one/jev.py](file:///Users/manmadeanyme/multica_workspaces_desktop-api.multica.ai/manmade-any-81b609a20e15/manm-258-6d7fdc64385d/workdir/ARES/system_one/jev.py)).
- Architectural decision record / assessment (ADR) from Architect confirming API compatibility, model version requirements, and SDK changelog.
- All unit and integration tests passing green.

## Acceptance Criteria
- `typesafe-sdk` upgraded to 0.7.5 in the environment and reflected in `requirements.txt`.
- Architect verification confirms whether default model `jev-1.13.0` remains active or requires bumping.
- All existing tests in `tests/unit/test_task210_*` and `tests/unit/test_task211_*` pass without deprecation or schema validation regressions.
- PR opened according to Global Development Pipeline standards.

## Edge Cases
- Breaking API or parameter changes between `typesafe-sdk` 0.7.2 and 0.7.5 (e.g., changes to `Choice`, `Score`, `Noul`, `TypeSafeClient`).
- Incompatible response model serialization or error type handling in `system_one/jev.py`.
- Network timeout or retry policy defaults adjusted in newer SDK versions.
