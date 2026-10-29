# TASK-20261023: Notify T2 touches while the ATR runner remains active

Status: user approved alert-only behavior on 2026-10-23.

## Contract

Every newly registered manual/reference trade using the spot ATR runner is enrolled for one `T2_TOUCHED` milestone. Pre-existing trades remain gated until individually reviewed. The separately gated option ATR policy retains its own T2 lifecycle and accounting.
Detect an inclusive favorable touch from live spot or completed-candle extremes,
using the existing entry-time wick safeguards. If a sample reaches T1 and T2,
record and notify T1 first, then T2. Closed trades and pre-entry candles cannot
create a milestone. A T2 touch never closes a trade, changes its stop, books
quantity, changes its outcome score, or calls `AnalyticsLogger.log_exit`.

Completed-history monitoring must inspect valid eligible T2 wicks even when an
ATR group has missing minutes, and before evaluating any later ATR close-through.
Tick milestones require a fresh receipt timestamp after entry and the runner's
watermark; existing stop monitoring continues if the tick is stale. T2 alerts
are queued per trade so Discord latency cannot block a protective exit.
Main calls ATR monitoring before its ordinary
latest-candle update; deferring the milestone until that later update would lose
a touch on the runner's exit candle. Include a source minute overlapping T1
activation when it ends after activation, while retaining entry-minute wick
protection. Fully pre-activation minutes remain excluded.

Keep legacy `T2_HIT` terminal records and alert wording compatible. The new event
must explicitly say that the runner remains active under ATR. Reuse the existing
Discord trade-update card and show the spot T2 level, touch time, entry, active
stop and runner status. Option target references must not be described as an
observed premium touch or an executed exit.

## Persistence

Store the milestone in `trade_analytics.market_context.target_milestones.t2`, with
the observation time reference, its time basis, target spot price, and signed spot points from entry. A candle timestamp is its start time and does not identify the exact intrabar touch. The points are a reference, not realized P&L. Existing context restoration
loads the marker on restart. Use the existing ordered per-trade writer and context
lock, merge only owned keys, and preserve ATR and execution context. No schema
migration or production backfill is required. A repeated tick, candle, or restart
with the persisted milestone must not generate another T2 event. Follow current
trade-update delivery semantics; exactly-once external webhook delivery is not
claimed. Persistence failures must remain recoverable on subsequent monitoring
without generating repeated successful alerts in the same process.

## Files and verification

- `position_manager.py`: detect T2 for active runners, including the same sample
  as T1; persist a deep snapshot of milestone metadata through the ordered writer.
- `alerts.py`: render `T2_TOUCHED` as a notification with an active runner, leaving
  historical terminal `T2_HIT` behavior intact.
- Tests: bullish/bearish inclusive tick and wick touches; same-sample T1/T2 order;
  pre-entry protection; repeated observations; restart restoration; persistence
  failure recovery; unchanged ATR ratcheting and terminal accounting; alert content.
- Documentation: update README, original manual-runner ADR, and CHANGELOG in the
  same branch. Publish one draft PR for human review; do not merge or deploy.

## Scope

ATR calculation and close-based exit policy retain their existing behavior.
The chart indicator comparison is a separate discussion awaiting its settings.
