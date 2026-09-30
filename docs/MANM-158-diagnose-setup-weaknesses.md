# MANM-158: OI wall retest diagnosis

**Audit date:** 2026-09-30
**Status:** OI wall rule implemented and regression-tested; out-of-sample validation pending; failed breakout deferred

Read-only Supabase queries reproduced the ticket's September 11 baseline: 20 OI-wall trades lost 143.20 spot points (-7.16/trade). The 30.0% headline “win rate” counts breakeven exits; only 2 of 20 had positive PnL. Failed breakout baseline was 17 trades, -51.55, but that setup is deferred for this phase.

PR #119 introduced immediate entry when a re-test candle approaches the wall and closes on its defended side. The only subsequent OI-wall signal, September 28 signal 518, met the wall-size, OI-change, persistence, initial-interaction, and excursion checks. Its bullish PE 22950 retest candle then fell from 22966.85 to 22954.10, closing near its low. Because that close was 4.10 points above the wall, the filter qualified it. The next candle crossed the wall; the recorded trade stopped at -16.00 points. Suggested option sizing was zero lots, so this is an emitted signal and analytics trade, not proof of funded execution.

A filter-only replay of the recorded candles reproduced the qualification under PR #119. The earlier PR #103/#107 confirmation rule did not emit before the wall broke. PR #119 intentionally removed that later confirmation. Its existing PE lifecycle test even expects a falling retest candle to qualify, so the implementation matches the current test; the definition of “rejection” is the gap.

The implemented fix keeps immediate retest entry but requires the retest candle to move in the trade direction: `close > open` for bullish PE, `close < open` for bearish CE, in addition to the current wall-band and defended-close checks. A contrary candle keeps the wall ready for a later valid retest. This uses the same directional condition as the first interaction and adds no fitted threshold. The September 28 regression now waits at 09:19 and expires on the 09:20 wall breach. PE and CE mirror tests also cover later valid entries. The wall's OI growth does not, by itself, prove price defense, so stale OI and market-regime explanations remain hypotheses.

Stop widening is not part of this change. In the historical linked sample, 12/19 OI-wall stops sat on or before the wall; a hypothetical 6-point wall buffer raises average risk to 27.51 points and fails the current 1:1 gate for 10/19 signals. The time-corrected July scratch replay rescued no first-stop wall trades with that buffer. Full historical option chains and levels are unavailable, and the proposed date splits were chosen after inspecting the ticket sample. Thus the required out-of-sample expectancy validation remains **unmet**. Freeze the directional rule and compare it with the baseline on complete later replay data or a prospective shadow period. After merging the latest mainline changes, the full test suite passed (830 tests with `--import-mode=importlib`); tests establish behavior, not expectancy.

See [the MANM-158 ADR](../directives/adr/MANM-158_diagnose_setup_weaknesses.md) for query method, detailed evidence, limitations, and the approval boundary.
