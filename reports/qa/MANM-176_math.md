# MANM-176 option math QA

Date: 2026-10-16

Status: PASS for pricing, sizing, and snapshot contracts covered below. Live broker execution is outside this review.

## Verification

`python -m pytest tests/unit/test_options_math.py tests/unit/test_storage.py tests/unit/test_position_manager.py tests/unit/test_oi_fetcher.py tests/unit/test_options_alert_reporting.py -q`

Result: **160 passed**, 12 existing Supabase client deprecation warnings. Math suite contributes 80 cases. `git diff --check` passed for the owned files.

New regression tests first demonstrated missing report aliases, unsafe legacy lot values, incompatible persisted summary text, a decimal overflow exception, and execution allowed at expiry close. These failures passed after the fixes.

## Verified contracts

- Signed CE/PE delta and gamma projections preserve spot stop/target geometry. Gamma cannot lower the linear risk-sizing floor.
- Theta/vega scenario projection uses explicit holding days and IV percentage-point change. Expected holding time gates reward feasibility without altering instantaneous bracket levels or risk allocation.
- Capital is fetched from the broker; zero is preserved, failure produces no allocation, and configured `default_capital` is never substituted. No missing premium, delta, gamma, theta, vega, or IV is synthesized.
- Positive finite capital, integral lot size, bounded risk percentage, lot cap, affordability, spread, and rounded level ordering constrain executable lots.
- Earliest current expiry is selected before quote eligibility; invalid current-expiry quotes do not silently select a later expiry. Expiry-close execution is rejected at 15:30 IST. Naive application timestamps mean IST; aware timestamps are converted to IST.
- Legacy snapshots preserve capital, delta, premium, stop, target, risk percentage, and valid integral lots. Persisted summaries retain `Option Sizing: {lots} lots`.
- Full-chain keys produced by ingestion match math inputs. Invalid/missing Greeks reject execution. A missing bid/ask uses the specified LTP-plus-buffer entry rule, not fabricated quotes.
- Expected straddle move remains available with missing IV, but execution remains invalid. IV expected move uses spot × IV/100 × sqrt(calendar seconds to expiry close / (365 × 86400)). A discrepancy exceeding 25% of the straddle estimate adds non-blocking `IV_FEED_STALE` metadata.

## Limits

These are local Taylor/scenario estimates, not a guarantee of future option loss, liquidity, fill price, or broker acceptance. Straddle comparison is diagnostic; it does not prove feed staleness. Contract freshness beyond expiry is owned by ingestion/execution. No automatic elapsed-time exit is introduced.
