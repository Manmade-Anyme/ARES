# Legacy IV-Gated Breakout Prototype Audit

> **Scope:** This document describes the offline fixed-window prototype only. Its afternoon range, entry window, and results are not the live rolling IV-Gated Breakout or Trend Expansion strategies, and must not be used as evidence of their performance.

## Live detector behavior

The production IV-Gated Breakout uses a rolling 25-candle base throughout regular market hours (09:15–15:30 IST). It requires a price/volume/VWAP breakout and valid ATM Call IV. The IV check needs fresh same-strike samples near both one and three minutes before the candidate; a missing, stale, invalid, or rolled-strike baseline rejects the candidate with `NO_IV_DATA`.

The separate Trend Expansion detector also evaluates through regular market hours. Its current rules are in [the production specification](./POC_Trend_Expansion_Strategy_Validation.md).

## Offline prototype and data

- **Audit period:** 2026-08-07 to 2026-11-05 (62 cached sessions).
- **Inputs:** Cached one-minute NIFTY spot and Dhan ATM Call IV observations.
- **Runner:** `PYTHONPATH=. python -m poc.run_iv_breakout_audit`
- **Prototype scope:** Fixed historical afternoon range and entry windows in `poc/iv_breakout_expansion_poc.py`. The runner is retained for reproducibility, not as the live implementation.
- **IV units:** Dhan IV values and all `dIV` thresholds/deltas are in IV points, not percent changes. The prototype anti-crush thresholds are -0.30 IV points over one minute and -0.40 IV points over three minutes.
- **Data handling:** Missing IV or either exact same-strike comparison sample rejects the candidate. Option exit marks are accepted only from the entry strike; if unavailable, option PnL is left unpriced.

## Latest cached replay

The run screens 33 price candidates. The unfiltered prototype takes 20 trades; its IV-filtered replay takes 14. The rejections include candidates with missing comparison data, so rejected outcomes cannot be presented as confirmed fakeouts or as a live-strategy win-rate improvement.

| Metric | Baseline prototype | IV-filtered prototype | Difference |
|---|---:|---:|---:|
| Spot trades | 20 | 14 | -6 |
| Spot wins / losses | 8 / 12 | 6 / 8 | -2 wins / -4 losses |
| Spot PnL | -48.40 pts | -95.85 pts | -47.45 pts |
| Option premium PnL | -89.70 pts | -103.15 pts | -13.45 pts |
| Rejected candidates | — | 19 | — |
| Rejected candidates with non-positive / positive hypothetical spot PnL | — | 10 / 9 | -8.40 pts net hypothetical impact |

These are retrospective outputs from a legacy, fixed-window simulator. They do not establish that the current live strategy is profitable, nor that missing-data rejections were correct market decisions.

## Implementation references

- `options_math.py`: shared IV velocity evaluation and fresh-sample selection.
- `detectors/iv_breakout.py`: production rolling IV-Gated Breakout.
- `detectors/trend_expansion.py`: production rolling Trend Expansion.
- `poc/iv_breakout_expansion_poc.py`: legacy offline prototype and audit mechanics.
- `poc/run_iv_breakout_audit.py`: cached replay summary.
