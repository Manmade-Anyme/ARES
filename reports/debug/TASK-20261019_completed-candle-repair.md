# ARES completed-candle repair — 2026-10-19

## Scope and decision

The user selected: **fix candle handling while keeping Friday's strategy rules**, with deployment and live verification before the 15:30 IST market close. This report separates the initial repair's predeployment and live production evidence from the subsequent review fixes prepared after the close.

An exact rollback to Friday commit `5729277` would retain the candle defect. Comparison with deployed base `4e7e869` found the same fetcher, engine and detector behavior; the relevant configuration difference was three options execution fields. The repair therefore changes the market-data contract without changing detector thresholds, confidence calibration, expiry rules or options execution rules.

## Proven failure and repair

The prior fetcher selected the final API row without proving that its one-minute interval had closed. It also built VWAP from observations accumulated after startup, rather than completed history from the beginning of the trading session. The diagnosis replay reproduced zero candidates on recorded partial inputs and a qualifying bearish continuation candidate using completed inputs under unchanged strategy rules. That detector-only result does not establish a guaranteed live trade: historical full-chain data and complete detector priority state were unavailable.

The initial runtime repair touched three files:

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

## PR #140 first review remediation — `e088829`

The following fixes were prepared after the 19 October close and recorded in `e088829`. They are **not deployed** and must not inherit the release 168 live-verification claim above. Friday's detector conditions, thresholds, confidence calibration and option calculation rules remain unchanged. The tests in this section predate the second review findings below.

| Review finding | Remediation |
|---|---|
| [Reject malformed completed rows before falling back](https://github.com/Manmade-Anyme/ARES/pull/140#discussion_r4312323892) | Trading normalization validates every completed current-session row, including historical rows and duplicate revisions, before selection or VWAP accumulation. Unknown timestamps reject the response. Validation failure preserves the previous session date, emission marker, watch history and VWAP state. Default watch parsing remains forgiving. |
| [Preserve processing of the final completed session bar](https://github.com/Manmade-Anyme/ARES/pull/140#discussion_r4312323901) | An existing current-day session gets one final fetch during 15:30–15:31 IST, accepting only its completed 15:29 bar. The fetch, expiry lookup and chain fetch each have a separate 10-second await limit. High/low protective exits run before chain I/O. Available chain data allows final detector/ML processing, but candidates are analysis only: `SESSION_CLOSED`, zero lots, no bracket, entry sizing, dispatch, pending quote fill, new watch heads-up or entry alert. |
| [Preserve REST risk checks when no candle completes](https://github.com/Manmade-Anyme/ARES/pull/140#discussion_r4312385369) | With no new candle and an inactive WebSocket, an independent fresh REST LTP drives exits, stop-out cooldown clearing and pending-order invalidation before optional chain I/O. Fresh quotes update pending paper orders only before the close. No detector candle or ML snapshot is fabricated. |

Fresh clock checks fence requests that cross 15:30; the loop proceeds directly to the close gate rather than sleeping through the finalization window. Cold starts, missing/invalid final candles, duplicates and stale bars do not replay the session. Finalization is attempted once, including failure paths. Nested cleanup blocks guarantee that terminal-watch cleanup failure cannot skip outbox closure or tick-feed stop, and outbox closure failure cannot skip tick-feed stop.

Worker regression evidence, with overlapping counts kept separate:

- Fetcher reproduction: **26 failed, 18 passed** before implementation; final focused fetcher/helper suite **136 passed in 28.92s**. Evidence: `/tmp/ares-pr140-fetcher-red.txt`, `/tmp/ares-pr140-fetcher-focused.txt` and `/tmp/ares-pr140-fetcher-fix.md`.
- Main-loop reproduction: **9 failed, 2 passed** before implementation. Additional QA regressions reproduced cleanup failures and a request sleeping past the finalization window. Final focused main-loop suite: **36 passed in 1.21s**, including real timeout cancellation and pending paper-fill behavior. Evidence: `/tmp/ares-pr140-main-fix.md`.
- Captured-data check: replayed provider history sampled at **14:49:53 IST** selected the **14:48** bar, used **334 completed bars**, and calculated VWAP **23,001.499725785998**. An immediate duplicate was ignored; a malformed latest completed row with valid older history was rejected without changing state. Evidence: `/tmp/ares-pr140-captured-review-validation.json`. This is offline replay of captured 19 October data, not a new live-session test.
- Full-session replay: provider history fetched at **16:47:40 IST** contained **376 bars**. At simulated poll times **15:29:59** and **15:30:06**, the patched fetcher selected **15:28** and **15:29** respectively. It included **375 unique regular-session bars**, excluded the extra 15:30 bar, matched independent session VWAP **22,982.710338791112** exactly and ignored a repeated poll. Evidence: `/tmp/ares-pr140-full-session-validation.json`. These are corrected historical bars fetched after the close; the result does not establish that the final bar was available live at 15:30.
- Final integrated suite: `python3 -m pytest tests/ -q --import-mode=importlib --disable-warnings --tb=short` — **1,537 passed, 12 warnings; 45 subtests passed in 57.36s**. Evidence: `/tmp/ares-pr140-final-full-suite.txt`. Independent QA approved the follow-up. These counts overlap the focused runs and must not be added to them.

Deployment of these review fixes remains pending. The original three production polls above validate only commit `a1031b36c4db2813db3b23af40d77ffb5fa0c883`; the later full-suite and captured-data checks are offline verification.

## PR #140 second review — `2396302`, verified offline

At **17:00:05 IST on 19 October** (`11:30:05 UTC`), a fresh automated review identified two additional valid failures. The remediation below was recorded in `2396302`, its integrated suite passed and independent QA approved it. These historical checks superseded the earlier 1,537-test result for that commit; they do not verify the third review change below. No deployment or new live-session verification is claimed.

| Review finding | Implemented remediation |
|---|---|
| [P1: deduplication precedes downstream processing](https://github.com/Manmade-Anyme/ARES/pull/140#discussion_r4313281360) | Main requests deferred acknowledgment so external prerequisite failures do not consume a still-latest completed bar. Acknowledgment occurs after chain and level preparation, immediately before the stateful engine call, preserving at-most-once processing even if the tick partially mutates state and raises. Direct fetcher callers retain automatic acknowledgment by default. A newer completed bar may intentionally supersede an older unconsumed bar; no backlog replay is introduced. |
| [P2: final nonsignal ML work can be lost at shutdown](https://github.com/Manmade-Anyme/ARES/pull/140#discussion_r4313281368) | Main and its snapshot helper propagate `wait_for_completion=True` to the real collector for final and crossed-close snapshots, including nonsignal rows. The caller awaits the actual executor-backed insert attempt before shutdown. The helper forwards the new keyword only when True, preserving the ordinary collector call shape and background behavior; existing signal-bound upsert/retry behavior is unchanged. |

The acknowledgment API accepts only a typed `OHLCVCandle` with a datetime timestamp, normalizes aware time to naive IST, requires an initialized matching source session and the regular 09:15–15:30 window, and checks that the timestamp exists in validated completed provider watch rows. Foreign, unfinished and fabricated acknowledgments cannot suppress a valid candle. Valid acknowledgments remain monotonic and idempotent. A fresh bottom-of-loop clock guard also handles a chain request that crosses the close and raises, advancing directly to finalization rather than sleeping through its permitted window.

Focused red/green evidence, with overlapping runs kept separate:

- Initial acknowledgment/helper/main-loop regressions: **6 failed, 72 passed** before implementation. The first implementation's acknowledgment after engine return was superseded during integration review; final source acknowledges immediately before the stateful call.
- Actual `AresEngine` regression: missing ATM data appended the candle and then raised. Before the handoff correction, the same provider bar appended twice; the fixed path leaves one buffer row and suppresses the duplicate fetch. The combined acknowledgment-boundary red run was **4 failed, 8 passed**, also exposing invalid future-day, out-of-session and unfinished acknowledgments.
- Collector completion contract: **2 failed, 1 passed** before implementation; real collector/threaded-insert barrier suite **47 passed and 8 subtests passed in 1.55s**. Controlled inserts prove the requested barrier waits, the ordinary default remains background, and insertion exceptions propagate to the caller's error handling.
- Ordinary-call compatibility: an independent strict snapshot wrapper failed when the first helper revision forwarded `wait_for_completion=False`. A regression reproduced the failure; the corrected helper omits the new keyword unless True and the wrapper executes normally.
- Final integration-focused suite: **155 passed and 8 subtests passed in 1.37s**, superseding the earlier 154-test run after the compatibility correction. Causal run-loop tests pin completion of the specific **15:29** snapshot before `tick_feed.stop()`; an earlier 15:28 completion cannot satisfy the assertion. Tests also cover same-bar pre-engine retries, no duplicate state mutation after an engine exception and the exceptional request-crossing-close path. This is a focused result, not the full-suite count.
- Final integrated suite: `python3 -m pytest tests/ -q --import-mode=importlib --disable-warnings --tb=short` — **1,559 passed, 12 warnings; 45 subtests passed in 56.08s**. Evidence: `/tmp/ares-pr140-followup2-final-full-suite.txt`.
- Independent QA: **APPROVE**, with **13 causal harness cases passed** and **137 focused tests plus 8 subtests passed in 1.32s**. Evidence: `/tmp/ares-pr140-followup2-qa.md`. Focused runs overlap the full suite; counts must not be summed.
- Deferred-acknowledgment captured-data replay: the completed **15:29** bar at simulated **15:30:06** used **375 regular-session bars**, matching session VWAP **22,982.710338791112**. The same latest bar was returned repeatedly before acknowledgment and suppressed afterward. Evidence: `/tmp/ares-pr140-deferred-ack-captured-validation.json`. This is offline replay of corrected historical data fetched after the close, not proof of live final-bar availability.

Additional implementation evidence: `/tmp/ares-pr140-new-review-findings.json`, `/tmp/ares-pr140-followup2-final.md` and `/tmp/ares-pr140-collector-barrier.md`. The final integration report supersedes the earlier implementation report's acknowledgment position. Both `e088829` and this follow-up were prepared after the close and remain undeployed.

For a future rollout, use the normal full build after human merge, or include all four changed runtime files relative to the initial base: `main.py`, `fetchers/price_fetcher.py`, `oi_watch_context.py` and `ml_signal/collector.py`. Release 168's original three-file overlay does not contain the collector completion fix.

## PR #140 third review — activity eligibility verified offline

[P2: allow the final drain after the first pre-close poll fails](https://github.com/Manmade-Anyme/ARES/pull/140#discussion_r4317190680) identified a remaining gap in `2396302`. The close gate inferred an active session from `last_watch_candle`, which is set only after a successful engine tick. A first market-open poll could therefore cross the close during a price, expiry, chain or level failure, or a no-new-candle response, and then be misclassified as an after-hours cold start.

The minimal correction separates **current-day market-open poll activity** from the last successfully processed candle. Activity is marked when an open-session poll begins, before external awaits. That activity date permits the same single final fetch within 15:30–15:31 even when no earlier engine tick succeeded. True after-hours starts without an open-session poll remain ineligible; prior-day activity and an already processed 15:29 bar do not permit another drain. Final-bar validation, bounded awaits, at-most-once engine handoff and analysis-only entry suppression remain unchanged.

The authentication recovery branch retains credential reload and error alerts, but checks the fresh clock before its 60-second retry sleep. At or after the close it skips that backoff and reaches the final gate, so the first authentication failure cannot consume the grace window after already crossing 15:30.

Offline regression and review evidence:

- Before the activity marker correction: **5 failed, 1 passed** in the targeted worker reproduction; the passing control excluded prior-day-only activity. A further causal test reproduced the authentication backoff still sleeping after close, then passed with the fresh-clock guard.
- Implementation-focused suite: **162 passed and 8 subtests passed in 1.61s**. Evidence: `/tmp/ares-pr140-session-activity-fix.md`.
- Independent QA: **APPROVE**, with **11 causal cases passed**. The `2396302` baseline produced **7 expected failures and 4 passing controls**; the fixed source passes all 11. Recovery cases advance the clock inside the first failed/missing-data callback, then verify exactly one final 15:29 engine attempt, protective exits, snapshot handling, no post-close sleep or entries, and cleanup. Cold-start, prior-day-only and already-processed-final-bar controls stay excluded.
- Independent focused suite: **145 passed and 8 subtests passed in 1.59s**. Evidence: `/tmp/ares-pr140-session-activity-qa.md`. These focused runs overlap and must not be summed.
- Final integrated suite: `python3 -m pytest tests/ -q --import-mode=importlib --disable-warnings --tb=short` — **1,566 passed, 12 warnings; 45 subtests passed in 56.46s**. Evidence: `/tmp/ares-pr140-session-activity-full-suite.txt`. The focused results above overlap this full run and are not additional test counts.

This final integrated result verifies the frozen activity/authentication correction offline; the preceding 1,559-test result and green CI remain historical evidence for `2396302`. No deployment or live-session result is claimed for the review follow-ups.

