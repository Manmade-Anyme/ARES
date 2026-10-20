# Compact Discord Option Alerts

- `discord_show_option_details=False` in both profiles hides only Order Type, Option Greeks and Risk Budget; `True` restores them.
- Discord shows premium-only Stop Loss in both modes. Console retains detailed diagnostics and both stop layers.
- Tests exercise funded and zero/insufficient-capital signals in both modes, preserving entry, SL, targets and lots.
- Focused checks: 68 tests passed. Full regression: 1,612 tests and 45 subtests passed with `python3 -m pytest tests/ -q --import-mode=importlib` (avoids an installed `tests` package shadowing the local namespace).
- All changed executable lines and changed-source branches covered; `config_profiles.py` is 100% line/branch covered. Existing unrelated alert gaps remain; whole-file coverage is 92%.
- Read-only QA/review found no actionable issues; mirrored ADR/docs match. No live Discord message was sent.
- CI validation remains pending the new PR run.
