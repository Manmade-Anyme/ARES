# Research note — Jev inside the first OI-wall watch

Date: 2026-10-08. Status: proposal only; no application, configuration, database or deployment changes.

## Requested behavior

Preserve the existing OI_WALL_RETEST_READY Discord embed, including title, time, spot, wall, persistence, guidance and footer. Append one field named “🧠 JEV Prediction” before the footer. Include the assessment in the first delivered watch message. No separate Jev follow-up for this event. Existing confirmed-signal Jev messages remain outside this scope.

The main output is the estimated probability that the session ends materially further in the watch direction than the captured watch spot. Define the neutral band in spot points or volatility units before testing; persist its numeric value with each prediction. This measures net continuation into the close, not uninterrupted movement or option-premium profitability. A separate, precisely defined path-reversal question can distinguish an initial opportunity followed by a return to the watch reference.

Display template, not an actual historical prediction:

> Day-close continuation: {probability}% — {direction}
> Outlook: {typed trend assessment}
> Key evidence: {short template grounded in supplied features}
> Assessment as of: {snapshot time} · Experimental estimate

Insufficient evidence and service unavailability must be explicit. Do not manufacture a percentage for today's 09:28 alert using knowledge of its later decline.

## Verified integration constraints

- `alerts.py:187` builds the current watch embed; `dispatch_oi_wall_watch_alerts` delivers cancellations before a fresh watch and acknowledges successful delivery.
- `main.py:341` sends watches before `_record_ml_snapshot` around line 444. Existing persisted snapshots cannot simply be assumed available when the first watch is sent.
- `system_one/context.py:88` requires a confirmed signal with valid T1/T2/SL geometry. A watch needs its own context, without invented barriers or trade records.
- `system_one/jev.py` currently asks T1-versus-SL and conditional T2 questions. These outputs cannot be relabeled day-continuation probabilities.
- The existing Jev worker is independently deployed and polls signal-bound records. `llm_predictions` and jobs are keyed to a real signal UUID, with a signal foreign key. Watch forecasts need event-aware persistence.
- `OIWallBias.wall_key` identifies a strike/side, not a unique watch episode. Delayed work needs a unique episode ID and lifecycle checks.

## Inputs

Start with captured wall size, growth, relative rank, persistence and interaction; available price candles; previous-day levels and current support/resistance; option-chain OI, IV and expiry context. Summarize the price sequence, rather than supplying only the latest candle.

Useful derived additions: 5/15/30-minute returns when available, higher/lower swings, realized volatility, session-so-far range position, opening gap, completed opening range, nearby opposing OI walls, and same-strike OI history. Preserve timestamps and distinguish observed history from missing warmup.

VWAP and participation require verified data provenance. `compute_candle_features` currently emits zero VWAP distance when VWAP is absent; the new context must not present that placeholder as evidence that price is at VWAP. Similar history-default values require availability flags. An index is not itself traded; validate any volume/VWAP source or label a futures proxy explicitly.

Optional later feeds: weighted constituent breadth, BANKNIFTY alignment, futures price/OI/basis, India VIX, and scheduled event context known before the alert. These are not prerequisites for the first version. OI growth alone does not identify buyers versus writers; option Greeks alone do not establish signed dealer positioning.

## Delivery options

1. Inline inference before the existing send: fewest apparent edits, but awaiting a network call in the trading loop delays other work and crosses the current worker boundary. Not preferred.
2. Capture a watch event and frozen snapshot, extend the independent Jev worker to process it, then deliver the existing embed plus the result through one alert owner: preserves the first-message requirement and avoids blocking trading. Recommended design direction, reusing existing inference and delivery machinery where compatible.
3. Send immediately and edit the message afterward: retains one message, but the first delivered version lacks the forecast. Does not satisfy the stated requirement.

Option 2 requires a bounded total waiting deadline, including queue time. On expiry/error, deliver the original watch with “Jev assessment unavailable” in the added field, without a later follow-up. Recheck the watch episode before dispatch so an expired/replaced watch cannot be sent with a late result. Synchronize successful-delivery acknowledgment with the existing cancellation lifecycle; only one sender owns each episode. Retain existing cancellation wording. Ambiguous webhook outcomes must not trigger blind duplicate sends.

If the watch ends while its webhook request is in flight, record any accepted delivery against the original immutable episode and then queue its cancellation. The current filter's acknowledgment checks the current state/key and cannot alone handle that asynchronous race. Independent read-only review confirmed this requirement.

## Implementation scope, if later authorized

Add watch snapshot/episode identity at the existing notification boundary; add a watch-specific Jev context and question version; extend worker routing and storage for watch predictions; append one embed field; connect timeout, lifecycle and delivery acknowledgment. Reuse the installed TypeSafe SDK and existing credentials. Detector thresholds, entries, SL, targets and trade management do not need changing for this feature.

Persist exact input, observation/availability timestamps, model/question version, raw probabilities, timing and delivery status. Add end-of-day outcome labels separately after close. Verify one combined message, unchanged original fields, bullish/bearish symmetry, early-session missing history, no future-data leakage, timeout behavior, canceled/replaced events, retries and restart recovery.

The prior audit found 6/7 watch-ready episodes favorable after two hours but only 4/7 at the recorded session endpoint, across six days. This motivates the question but is far too small to validate Jev probabilities. Evaluate prospectively against simple baseline forecasts, including calibration and performance on distinct held-out days. Historical replay is exploratory and is limited by missing contemporaneous inputs and hindsight/model contamination.

## Sources

- Local files cited above; `migrations/2026-10-01-task210-llm-predictions.sql`; `detectors/oi_wall_entry.py`; `ml_signal/features.py`.
- [Existing full-session audit](../../reports/research/TASK-011_oi-wall-eod-and-legacy-2026-10-08.md).
- [TypeSafe Choice](https://docs.typesafe.ai/primitives/choice): typed outcome probabilities.
- [TypeSafe confidence](https://docs.typesafe.ai/confidence): confidence summarizes the answer distribution; thresholds need domain testing. It is not demonstrated financial forecast accuracy.

Jev provides typed judgments rather than free-form explanations. Human-readable evidence text should be generated from grounded feature values and typed evidence judgments, not presented as an invented model rationale.
