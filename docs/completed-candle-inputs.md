# Completed candle input contract

`PriceFetcher.fetch_latest_candle()` returns the latest newly completed one-minute
bar from the current IST trading session, or `None` when no fresh completed bar is
available. Candle timestamps denote bar starts and remain naive IST for existing
engine callers. A bar is complete only when its start plus one minute is no later
than the response sampling time. Future, prior-session and out-of-session bars do
not enter the engine. Duplicate timestamps retain the latest provider revision
only after the completed rows have passed validation.

VWAP is rebuilt on every usable response from all unique completed session bars
returned by Dhan, including history before process startup. Forming bars are
excluded. Revised provider rows replace prior values instead of accumulating
again. All-zero volume falls back to the selected bar's typical price. A session
change or explicit `reset_vwap()` clears the emitted-bar marker. A failed response
does not change the session date, emission marker, watch history or VWAP state,
including when a malformed response arrives on the next trading day.

The trading path uses `build_watch_candles(..., strict_completed=True)`. Any
malformed completed current-session row, including an older history row or a
duplicate revision, rejects the response before state changes. An unparseable
timestamp also rejects it because its session and completion status cannot be
established. Known forming rows with invalid OHLCV remain excluded. The watch
parser's default mode remains forgiving; valid forming evidence retains explicit
response-sample provenance. API errors and malformed arrays remain errors.

When no fresh completed bar is available, detector state and ML candle snapshots
do not advance. With an active WebSocket, the existing tick path monitors exits
and pending orders. With an inactive WebSocket, the loop fetches a fresh,
independent REST LTP through `PriceFetcher.fetch_latest_spot()`; it never
substitutes the completed candle's close. Spot exits, stop-out cooldown clearing
and pending-order invalidation run before optional option-chain requests, so a
chain outage does not prevent those spot risk checks. Fresh chain quotes update
pending paper orders only while the session is still open.

An already-running session gets one final candle-fetch attempt when the loop
reaches 15:30 IST and is still before 15:31. It requires a previously processed
current-day bar earlier than 15:29 and accepts only that day's completed 15:29
bar. Cold starts, `None`, duplicates, stale bars and later attempts do not replay
the session. The final fetch has a 10-second await limit; expiry lookup and chain
fetch each have their own 10-second limit. This is not a single 10-second limit
for the whole finalization path.

The final bar's high/low protective exit checks run before optional chain I/O.
When chain data is available, the final bar also reaches normal detector and ML
feature processing. A resulting candidate is persisted as analysis only with
`SESSION_CLOSED` sizing and trade binding, zero lots and no bracket. Finalization
does not perform entry sizing, order dispatch, pending quote fills, new watch
heads-ups or entry alerts. Fresh clock checks also suppress these entry actions
when an ordinary request crosses the close. Such a request advances directly to
the close gate instead of sleeping another poll interval and missing the final
bar window.

Shutdown attempts terminal-watch cleanup, outbox closure and tick-feed stop in
nested `finally` blocks. Failure of one cleanup stage cannot skip the later
stages.

Friday's detector conditions, thresholds, confidence calibration and option
calculation rules remain unchanged. The review follow-up adds validation,
monitoring and session-close safeguards. Detector buffers still require fresh
bars after a restart; historical candles are not replayed into live signal state.

Release 168 deployed the initial repair during the 19 October session. The review
follow-up described here was prepared after the close and has offline regression
and captured-data verification only; its production rollout is not implied by
the initial repair's live evidence.
