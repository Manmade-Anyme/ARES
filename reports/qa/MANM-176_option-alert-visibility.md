# Option R:R check removal — QA and review

- User explicitly requested removal of the check, without extra display logic.
- Runtime change: delete the two-line `INSUFFICIENT_OPTION_RR` rejection from `build_option_bracket`. All earlier proposed display/ordering changes were discarded.
- Regression: bullish/bearish theta-adjusted R:R below one retains the same prices, lot suggestion and validity as the ordinary scenario, with both zero and funded capital.
- Pricing formulas, capital allocation, ordering/missing-data validation and paper-only execution remain unchanged. ATR trailing remains deferred.
- Final local suite: **1,615 tests and 45 subtests passed**, 12 existing warnings, in 67.56 seconds. Command: `python3 -m pytest tests/ -q --import-mode=importlib --cov=options_math --cov=alerts --cov=execution.order_manager --cov=execution.signal_dispatch --cov-branch`.
- Entire `options_math`, `execution.order_manager` and `execution.signal_dispatch`: **100% line/branch coverage**. Runtime diff adds no executable lines. Whole `alerts` coverage remains 92% with unrelated existing gaps. Previous PR #142 recorded 1,612 tests plus 45 subtests; no stored `reports/qa/baseline.json` exists.
- Independent review: no actionable findings. Specification mirrors match and whitespace checks pass. GitHub CI and production deployment remain separate from local verification.
