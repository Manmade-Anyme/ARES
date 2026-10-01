# OI-wall watch cancellation

A Discord `RETEST_READY` watch means a wall is armed and awaiting a later qualifying retest. It is not an entry signal. Previously, an expired watch could remain apparently active in Discord. The cancellation alert closes that communication gap.

## Delivered-watch lifecycle

The entry filter records a watch as delivered only after Discord accepts the webhook. If that watched candidate expires, or its final signal persistence/delivery fails, it queues an immutable cancellation containing the original wall, watch-ready timestamp, cancellation candle timestamp and close, reason, and unique event ID.

| Event | Watch outcome |
| --- | --- |
| Wall disappears or no longer qualifies | Cancel delivered watch |
| Another qualifying strike replaces the wall | Cancel original watch |
| Candle closes beyond the wall on its invalid side | Cancel delivered watch |
| Engine rejects the qualified candidate at its R:R gate | Cancel delivered watch |
| First candle with a different date reaches the engine | Cancel prior-session watch and reset trading state |
| Engine emits signal and consumes candidate | Retain delivered watch until persistence and final Discord delivery resolve |
| Signal persistence and final Discord delivery succeed | Clear watch without cancellation; subsequent trade exits use existing position-management alerts |
| Signal persistence fails | Cancel watch; trade entry was aborted |
| Final signal Discord delivery returns false or raises | Cancel watch with guidance to check recorded signals and any existing trade |
| Cooldown or detector priority suppresses entry | Keep watch active; require a fresh retest under existing rules |
| Watch webhook never succeeded | No cancellation for that undelivered watch |

The embed title is **OI WALL WATCH CANCELLED**. Its fields show Wall, Watch armed, Cancelled at, Spot at cancellation, Reason, and Trader Guidance. Times display in IST. Guidance explicitly states that cancellation applies to the watch and does not close an existing trade. For final signal delivery failures it says: “Final signal alert was not delivered. Check recorded signals and any existing trade. This cancels the watch and does not close a trade.” A trade may already be tracked in that case; the alert does not claim that entry was aborted.

## Delivery and retry

`dispatch_oi_wall_watch_alerts()` sends queued cancellations in order before a fresh watch. A successful response acknowledges only that event ID. A failed response leaves the event queued and defers fresh watch delivery until a later dispatch succeeds. Candidate resets and session rollover retain the queue and original event details. Disabling new watch alerts does not prevent closure of an already delivered watch; a configured Discord webhook is still required.

Only the active delivered watch can enqueue its cancellation, and enqueueing clears that active record. Repeated expiry evaluations therefore do not create repeated cancellation events. After disappearance and fresh qualification, the same strike can produce a new watch. A consumed strike remains subject to the existing session consumption rule.

This is process-memory delivery state, without database persistence. Restarting ARES loses both active-watch delivery history and pending cancellation notices. An ambiguous network failure after Discord accepted a post can cause a duplicate on retry; the event ID stays the same. Session cancellation occurs on the next candle-date transition, not on a scheduled market-close timer. The main loop exits at 15:30; a next-day process restart cannot recover the previous watch, so this feature does not guarantee an end-of-day cancellation or a cancellation after restart. Alert delivery failures do not interrupt the trading or signal-persistence path.

## October 1, 2026 audit example

Recorded telemetry showed the **22,600 CE** wall become `RETEST_READY` at **09:19 IST** and `EXPIRED` at **09:20**, without entry. With this change, an acknowledged 09:19 watch would receive a cancellation explaining that the wall disappeared or no longer qualified.

The normal-profile retest distance is **20 spot points**. For a bearish 22,600 CE wall, a later retest candle needs `high >= 22,580`, `close <= 22,600`, and `close < open`, while the wall remains eligible. It need not reach exactly 22,600. The recorded 09:31 high of **22,577.25** was 2.75 points short of that band, and the candle was bullish. Independently, the original watch had already expired.

The initial interaction, later favourable excursion and secondary retest are separate state-machine steps, not a generic swing-high/swing-low detector. Bearish excursion is measured from the wall strike to a later candle low; bullish excursion mirrors this using a later candle high. The cancellation feature changes no wall thresholds, retest distances, entry prices, stops or targets.

The stored historical option-chain snapshots are incomplete. They support the recorded lifecycle and candle checks, but do not establish an exact OI calculation for every evaluation or predict a future wall touch.

## Acceptance summary

- Delivered watches receive a cancellation for each terminal path above; undelivered watches and successfully delivered final signals do not. Engine consumption alone does not resolve the watch.
- Replacement cancellations retain the original wall identity; retries retain the original time, spot, reason and event ID.
- Successful delivery removes that event; failed delivery survives candidate/session resets and runs before fresh watches.
- Existing retest qualification, risk levels and trade-exit behavior remain unchanged.

Implementation: `detectors/oi_wall_entry.py` owns lifecycle and pending notices; `models.py` defines the event; `alerts.py` formats and dispatches it; `main.py` invokes dispatch after engine evaluation and acknowledges final signal persistence/delivery; `engine.py` preserves the filter's delivery queue during date rollover.
