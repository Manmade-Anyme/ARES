# ARES completed-candle repair — 2026-10-19

## Scope and decision

The user selected: **fix candle handling while keeping Friday's strategy rules**, with deployment and live verification before the 15:30 IST market close. This report records the repair and predeployment evidence. Deployment completion and production observations must be recorded separately below after they occur.

An exact rollback to Friday commit `5729277` would retain the candle defect. Comparison with deployed base `4e7e869` found the same fetcher, engine and detector behavior; the relevant configuration difference was three options execution fields. The repair therefore changes the market-data contract without changing detector thresholds, confidence calibration, expiry rules or options execution rules.

## Proven failure and repair

The prior fetcher selected the final API row without proving that its one-minute interval had closed. It also built VWAP from observations accumulated after startup, rather than completed history from the beginning of the trading session. The diagnosis replay reproduced zero candidates on recorded partial inputs and a qualifying bearish continuation candidate using completed inputs under unchanged strategy rules. That detector-only result does not establish a guaranteed live trade: historical full-chain data and complete detector priority state were unavailable.

Runtime changes are limited to:

- `fetchers/price_fetcher.py`: select the latest newly completed candle from the current IST session; rebuild VWAP from unique completed provider bars from 09:15; return `None` when no newer completed candle is available. Timestamp revisions replace duplicate rows. Session rollover and reset clear emission state. Malformed provider arrays and unusable current-session OHLCV retain error handling.
- `oi_watch_context.py`: share timestamp normalization between trading inputs and watch evidence, including epoch seconds/milliseconds, numeric strings and timezone offsets. Preserve partial/final watch provenance.
- `main.py`: when the fetcher returns `None`, keep tick exit and pending paper-order monitoring active during the normal poll interval, without advancing detector state on repeated or unfinished bars.

Detector buffers still warm up after a process restart. Full-session VWAP reconstruction does not restore historical detector state or guarantee an immediate signal.

## Deterministic verification before deployment

The initial regression run against the previous implementation produced **12 failures and 3 passes**, including unfinished-candle selection, late-start VWAP, repeated/revised rows, timestamp normalization and session boundaries. During repair, malformed-provider cases were clarified to preserve the existing exception contract rather than silently treating corrupt data as an ordinary missing bar.

Final recorded checks:

| Check | Result | Evidence |
|---|---|---|
| Focused regression and affected behavior suite | 168 passed in 31.28s | `/tmp/ares-completed-final-focused.txt` |
| Full suite with importlib collection | 1,482 passed, 12 warnings; 45 subtests passed in 57.24s | `/tmp/ares-completed-full-importlib.txt` |
| Initial failing reproduction | 12 failed, 3 passed in 0.30s | `/tmp/ares-completed-red.txt` |

These runs overlap and their pass counts must not be summed. Regressions cover completed selection, full-session VWAP after late startup, duplicate/revised provider data, forming-only and out-of-session responses, supported timestamp formats, session rollover, zero-volume fallback, malformed responses and the main loop's no-new-bar path.

## Read-only live shadow before deployment

At **15:03:37 IST on 2026-10-19** (`09:33:37.006115 UTC`), an isolated diagnostic process loaded the patched code and queried current provider data. Production code was still unmodified. The process did not register orders, force signals or change strategy settings.

| Observation | Result |
|---|---|
| Selected completed candle | 15:02:00 IST; 97.006 seconds old |
| Completed session history | 348 bars, beginning 09:15:00 IST |
| Selected candle volume | 1,148,167 |
| Reconstructed VWAP | 22,991.587214902098 |
| Independent expected VWAP | 22,991.587214902098; exact match |
| Partial candles retained as watch evidence | 1 |
| Immediate second poll | Ignored (`None`), preventing duplicate detector advancement |

Evidence: `/tmp/ares-live-shadow-before-deploy.log`. This demonstrates correct selection and VWAP against live market data before release; it is not postdeployment production verification and does not prove that a live trade signal was emitted.

## Deployment and production verification

**Completed during market hours, 2026-10-19.** The user explicitly authorized the repair and selected unchanged Friday strategy rules.

- Source commit: `a1031b36c4db2813db3b23af40d77ffb5fa0c883`; [PR #140](https://github.com/Manmade-Anyme/ARES/pull/140). Independent QA review approved the patch. GitHub PR CI passed before rollout.
- Fly release **168**, existing machine `7812561a492198`, healthy and started. Machine update time **15:09:50 IST**; first successful app snapshot **15:10:06 IST**, before the 15:30 close. The machine ID and scheduled lifecycle configuration were preserved.
- Image: `registry.fly.io/ares-xzy-gq:completed-candles-a1031b36c4db-pinned`; digest `sha256:11e0e1faf52ac512c338ee70e95ab41761428ad0c54a1ef578f7d4e883011fda`.
- The image derives from the exact prior production image digest `sha256:30e2e5c619bf0793b9718fa378ac06287ac6408ce4b58dd1bc2c85db80a054f9` and overlays only the three reviewed runtime source files. This retains the existing dependency versions. Local build context bytes were compared with the published commit; SHA-256 checks of all three deployed files then matched that commit.
- A deployed-code read-only Dhan check at **15:10:50 IST** selected the completed 15:09 bar (110.894 seconds old), calculated VWAP from **355 completed session bars beginning 09:15**, retained one forming watch bar, and returned `None` on an immediate duplicate poll.

The running engine's persisted snapshots were independently matched against subsequent completed provider OHLCV and cumulative session VWAP:

| Engine poll IST | Completed source bar IST | Volume | Session VWAP | VWAP error |
|---|---|---:|---:|---:|
| 15:10:06 | 15:09 | 1,573,756 | 22,986.809335496815 | 0 |
| 15:11:06 | 15:10 | 1,946,743 | 22,985.861920402054 | 0 |
| 15:12:07 | 15:11 | 1,556,693 | 22,985.169073554065 | 0 |

These are three distinct completed bars, not repeated detector evaluations of a forming bar. Evidence is saved alongside this report in `TASK-20261019_live-validation.json`.

No signal was generated in these first three post-restart polls; this validates live input processing, not a guarantee of a new market setup. Detector buffers and regime persistence still warm up after restart. The bullish MEDIUM exhaustion signal `6365` at **15:05 IST** occurred on the old image, before rollout, and is not credited to this repair.

The emergency reviewed commit was deployed directly to meet the user's market-hours test requirement. The PR remains open for human merge under the repository's merge policy; merging it retains the deployed repair in `main` and invokes the normal CI pipeline.

