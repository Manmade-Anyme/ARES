# TASK-20261029: Spot T2 reference monitoring

Base: `782f6eede69f95dc4c5e4bb73e67a906d625e902` (main). Production was last verified on `9645663`; a subsequent Fly status refresh returned 503.

## Behavior

- Detect the first favorable T2 spot touch after T1, including a fresh tick, a same-sample T1/T2, or an eligible completed minute even when its five-minute ATR group has a gap.
- Persist target spot, signed points from entry, time reference, and time basis in `trade_analytics.market_context.target_milestones.t2`. Keep the runner open under spot ATR; no additional booking, terminal P&L, or score is invented. The separately gated option ATR policy is unchanged.
- Reject pre-entry, stale tick and pre-watermark candle observations. Process T2 minutes before later ATR closes and do not report touches after a runner exit.
- Queue per-trade alert snapshots in order without blocking protective exits. Graceful shutdown drains outstanding writes and alerts and retries a pending failed milestone once within a bounded deadline. Discord delivery remains best-effort.

## Verification

- The #0979 stored-candle regression failed against the unmodified base (no T2 event) and passed with this change. It uses target 22870.75, entry 22790.75 and a recorded high of 22874.15. The ML snapshot has collection time 10:31:49 IST; its embedded candle lacks a timestamp, so the exact source minute is not asserted from that row alone.
- Full Python suite: **1,963 passed, 45 subtests passed**, 12 dependency warnings (Python 3.14.6). Two option-policy tests initially failed on unmodified main during market hours because their defaultdict fixture was read with `.get`; seeding the intended availability value fixed the fixture.
- Local runtime replay used the actual position manager, Supabase Python client and tick-wait path against dummy loopback REST and Discord endpoints: persisted the #0979 milestone and +80 spot reference points, restored it after restart and suppressed a repeated fresh tick. No production service was contacted by the replay.
- Scoped review reproduced and verified corrections for stale ticks, a missing minute in an ATR group, alert latency delaying a stop, and shutdown cancellation or a failed pending marker. Final review found no further material issue in this scope.

## Limits

No production row, order, webhook, activation, deployment, or historical backfill was changed. A missed touch on #0979 is not automatically reconstructed; a future eligible touch may be recorded after release. A candle records its start as a time reference, not an exact touch time. The local REST server is a simulation, not live Supabase or Discord verification. A prolonged outage or hard process termination can still prevent best-effort delivery or persistence.
