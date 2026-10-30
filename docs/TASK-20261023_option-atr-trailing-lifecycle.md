# Option-Premium ATR Trailing, Milestone Accounting, and Discord Alerts

**Task:** TASK-20261023  
**Status:** PR #147 correction implementation complete; final validation recorded in QA report; production activation disabled
**Date:** 2026-10-23  
**Branch:** `feature/TASK-20261023-option-atr`  

---

## 1. Executive Summary

This architecture supersedes post-T1 spot-based ATR trailing with pinned option-premium ATR trailing on completed 5-minute candles. Initial entry calculations, Greeks limit pricing, risk budgeting, and spot levels (Index Entry, SL, T1, T2) remain unchanged. Index levels trigger T1 and T2 milestones; option stops govern the runner.

### Lifecycle & Milestone Accounting Matrix

| Event | Allocation & Protection | Recorded Score | Lifecycle Result State | Active Trades State |
|---|---|---:|---|---|
| **Before T1** | Original Index SL protects 100%. Option ATR inactive. | `NULL` | `OPEN` | `OPEN` |
| **Index SL Hit Before T1** | Close 100%. | `0` | `SL_HIT` | `CLOSED` |
| **Index T1 Reached** | Model-book 50%; 50% remains. Index SL moved to entry break-even. Option ATR management enabled. | `1` | `OPEN` | `T1_HIT` |
| **Index Entry Touched (T1-T2)** | Close remaining 50%. (Index break-even does not guarantee option break-even). | `1` | `STOPPED_OUT_AT_BE` | `CLOSED` |
| **Option ATR Exit (T1-T2)** | Completed 5m close strictly below active stop closes remaining 50%. | `1` | `ATR_SL_HIT` | `CLOSED` |
| **Index T2 Reached** | Model-book additional 30% (80% cumulative booked, 20% runner remains). Index SL protection disabled. Option fallback floor active at original option SL if ATR line not yet qualified. | `2` | `OPEN` | `T2_REACHED` |
| **Option Stop Exit After T2** | Completed 5m close strictly below active stop closes final 20%. | `2` (retained) | `ATR_SL_HIT` / `OPTION_SL_FLOOR_HIT` | `CLOSED` |

---

## 2. Mathematical Specification & Parity

### ATR TS Specification (5-minute timeframe)
- **ATR Period:** 5 (Wilder smoothing: $\text{ATR}_i = \frac{\text{ATR}_{i-1} \times 4 + \text{TR}_i}{5}$)
- **HHV Period:** 10
- **Multiplier:** 2.5
- **Candidate Indicator Formula:**
  $$\text{raw\_line}_i = \max_{k=0..9} \left( \text{High}_{i-k} - 2.5 \times \text{ATR5}_{i-k} \right)$$
- **Ratchet Rule:**
  $$\text{locked\_stop}_i = \max(\text{locked\_stop}_{i-1}, \text{raw\_line}_i)$$
  *(Only eligible when $\text{raw\_line} > \text{original\_option\_sl}$)*

---

## 3. Database State & RPC Procedure

### Canonical State (`trade_analytics.market_context.option_trailing`)
Versioned under `policy_version: "option_atr_v1"`, tracking:
- `contract`: Pinned `security_id`, `exchange_segment`, `expiry`, `strike`, `option_type`.
- `phase`: `PRE_T1` | `POST_T1` | `POST_T2` | `CLOSED`.
- `highest_milestone`: `0` | `1` | `2`.
- `booked_fraction`, `remaining_fraction`.
- `locked_option_sl`, `protection_mode`.
- `last_processed_option_bar_end`.
- `alert_events`: Durable immutable event snapshots, submission outcomes and acknowledgements. Discord webhooks cannot guarantee exactly-once delivery after ambiguous transport failures.

### Transactional RPC (`apply_option_lifecycle_transition`)
Stored procedure in `migrations/20261023_option_lifecycle_transition.sql` providing:
1. `SELECT FOR UPDATE` on `active_trades` and `trade_analytics`.
2. Revision checking preventing concurrent race conditions.
3. Event deduplication on `event_id`.
4. Score and state separation: `score` updated to 1 or 2 while `result_state` remains `'OPEN'`.
5. Retaining highest milestone score upon final terminal closure.

---

## 4. Discord message contract (Cards A–L)

The pure `alerts.render_option_lifecycle_event(event)` accepts an immutable snapshot:

```python
{
    "event_id": "stable unique persisted transition/event ID",
    "update_type": "T2_HIT",  # `type` is also accepted
    "event_time": "2026-10-23T10:16:00+05:30",
    "trade": {"id": "...", "display_id": "1950", "entry_price": 22552.1,
              "stop_loss": 22552.1, "target_1": 22527.1, "target_2": 22472.1,
              "market_context": {"options_sizing": {}}},
    "option_trailing": {"policy_version": "option_atr_v1", "phase": "POST_T2",
                        "highest_milestone": 2, "remaining_fraction": .2,
                        "contract": {"security_id": "51333", "expiry": "2026-10-27",
                                     "strike": 22550, "option_type": "PE",
                                     "exchange_segment": "NSE_FNO"}},
    "index_price": None,  # observed contemporaneous mark only, never inferred
    "previous_stop": 148.15455043953523,
    "candle": {"start_time": "...", "end_time": "...", "close": 147.5},
    "detail": "operational evidence where relevant"
}
```

Lifecycle transition cards B–L include trade/display ID, durable event ID, exact pinned contract/security/expiry, original event time converted to IST, phase/lifecycle, index mark explicitly in points, original option SL explicitly in rupee premium, index protection status, target-model-booked percentage, remaining percentage, and the reference/model disclaimer. Missing timestamps or marks display `unavailable`; wall-clock time and fabricated prices never substitute. Two decimal display never changes persisted comparison precision.

Card A reuses the existing rich initial signal card after entry registration commits. It adds trade ID, pinned contract, event time from `signal.timestamp`, original index protection, risk status, allocation and reference disclaimer; it does not post a duplicate initial lifecycle message. Live detector timestamps inherited from `PriceFetcher` are naive exchange-local candle times and are explicitly interpreted as IST at this Card A boundary; offset-aware timestamps are converted to IST. Durable lifecycle event timestamps still require an explicit offset and display `unavailable` when naive or missing. Its transport retains the existing initial signal delivery behavior, rather than claiming a second durable outbox event.

The shared disclaimer is: “Reference/model tracking; allocations and candle closes are not broker fills. Index break-even does not guarantee option break-even.” Suggested fractional allocations do not claim actual whole-lot fills.

| Card / event | Exact message content and event-specific evidence |
|---|---|
| A / `NEW_SIGNAL` | **New reference signal.** Original index entry / SL / T1 / T2 in points; frozen Greeks option entry / SL / T1 / T2 in ₹ premium; suggested lots and risk status; original index SL protects until T1; option ATR starts after index T1. This does not change signal eligibility, pricing or sizing. |
| B / `T1_HIT` | **T1 reached — 50% model-booked.** Score 1 recorded only after successful persistence; remaining 50%; index SL moved to entry; ATR active at its seeded locked stop, or waiting for confirmed raw line strictly above original option SL. No option-entry floor. |
| C / `ATR_ACTIVE` | **Option ATR protection activated.** Original option SL and new locked option SL; confirmed source candle interval; close strictly below previously active SL rule; remaining allocation and index guard status. Emit only for later eligibility; do not duplicate activation already included in Card B. |
| D / `OPTION_SL_RATCHETED` | **Option SL raised.** Previous → new locked stop in ₹ premium; completed source candle start/end; phase, remaining allocation and index guard status. Emit on increase only; falling or unchanged raw lines produce no stop card. |
| E / `T2_HIT` / `T2_REACHED` | **T2 reached — 80% model-booked, 20% runner active.** Additional 30% of original allocation; score 2 recorded; final 20% remains OPEN; index guard disabled; show active locked option ATR or original-option-SL fallback. Never show stale T1 as current phase. |
| F / `SL_HIT` | **Initial index SL hit — reference closed.** Targets booked 0%; remainder closed 100%; score 0; index trigger/observed mark in points. No option stop fill inferred. |
| G / `STOPPED_OUT_AT_BE` | **Index entry protection hit — reference closed.** Prior T1 target booking 50%; remainder closed 50%; score 1 retained. Explicit index break-even ≠ option break-even disclaimer. |
| H / `ATR_SL_HIT`, milestone 1 | **Option ATR close-through — reference closed.** Prior target booking 50%; remainder closed 50%; score 1 retained; previously active option stop, confirmed option close and completed candle interval. Wicks and equality ignored. |
| I / `ATR_SL_HIT` or `OPTION_SL_FLOOR_HIT`, milestone 2 | **Option ATR / original SL floor close-through — reference closed.** Prior target booking 80%; final remainder closed 20%; score 2 retained; stop mode, previously effective stop, confirmed premium close and candle interval; index guard disabled. Never label all stopped allocation as target-booked 100%. |
| J / `MONITORING_UNAVAILABLE` | **Option monitoring unavailable.** Reason, last valid candle end and retained locked stop, phase, remaining fraction, index protection. One warning per outage when more than 60 seconds past the expected candle close during market hours; no ordinary overnight warning. |
| K / `MONITORING_RESTORED` | **Option monitoring restored.** Recovered source interval/watermark and retained stop, phase and index protection. Process recovered bars chronologically; if recovery discovers an exit, send the exit with its original event time instead of an obsolete “restored/open” message. |
| L / `PERSISTENCE_FAILED` / `PERSISTENCE_FAILURE` | **Observed event — database pending.** Observed event/time/phase and failure detail; protected observed state remains in memory, recording unconfirmed. Omit recorded-score claims. Stop active-reference messages after an observed close even while persistence retries. |

The sender `await alerts.send_option_lifecycle_event(event)` returns `DELIVERED`, `FAILED` or `AMBIGUOUS` and never mutates the event. Successful HTTP response is delivered; definite connection/HTTP failure is failed; write/read timeout or uncertain network submission is ambiguous. Persist acknowledgements through the transactional outbox. Retry only definite failures according to the durable policy; do not blindly retry an ambiguous webhook submission or claim exactly-once transport. The compatibility `send_trade_update` retains legacy rendering for unversioned trades and rejects new-policy snapshots, which must use this persisted event path.

## 5. Activation, persistence and precision

Defaults are `option_atr_enabled=False` and `option_atr_approved_version=""`. Enrollment requires the enabled setting, explicit approval of formula version `option_atr_v1`, a backend-only `supabase_service_role_key`, and a successful runtime schema and privilege capability check. The legacy parity setting does not approve enrollment. The user has approved this formula for the reference model; this does not assert chart parity. Existing trades are not automatically enrolled.

`t1_at` and buffered exchange timestamps use the same clock, including for tick-driven T1. A buffered tick whose entire exchange-time second ends before T1 cannot trigger the POST_T1 entry guard. If a second-resolution tick overlaps the phase boundary and touches the guard, preserve an `ORDER_UNKNOWN` marker and keep option ATR protection active. A tick-driven ambiguity can be resolved only from certified complete, ordered source history; a candle-close boundary cannot be ordered against second-resolution ticks and remains unresolved without more precise evidence. Local receipt time is used only when source time is unavailable. This phase fence does not change pre-T1 initial stop or target evaluation.

During the live loop, replay buffered spot ticks together with completed spot minutes and pinned option bars in source-time order. This lets a T2 touch before a five-minute option ATR exit update the score before closure; a later tick cannot change a trade already closed by an earlier option bar. Persist target and tick-exit timestamps from source time, while retaining tick receipt time separately for diagnosis.

Calculate and persist full finite numerical values. Only display rounds to two decimals; neither quote-tick rounding nor six-decimal truncation changes stop thresholds. Both CE and PE are bought premium: the stop only rises. Seed T1 from the latest fully completed eligible historical line, compare a new candle's close with the previously effective stop, then raise the stop for subsequent candles. Continuous prior-session history does not reset overnight. Each enrolled trade fixes `indicator_history_from` to its entry date minus ten calendar days; later fetches retain that requested origin. A requested fetch date alone does not establish that the provider returned the same warmup prefix. Missing same-session source intervals prevent indicator/watermark advancement until recovered.

The optional `indicator_seed_start` records the exact first accepted source-candle start used to initialize Wilder smoothing. It travels through the existing lifecycle context writes and typed serialization. Common history preparation pins that source start once; for an unanchored trade already running in the current process, the first confirmed cached bar supplies the seed. Copy a cache-derived anchor into the context before rejecting a shortened response, so subsequent outage/context writes retain the trusted seed. Ignore newly returned candles before the pinned start, and reject a response missing that start before recalculating ATR, changing the confirmed cache or publishing premium availability. This rejection also applies when a shortened response still contains the durable processed-bar watermark: retain the prior stop and watermark until the original prefix returns, then resume its calculation. Pre-T1 seeding uses the same anchor. For an older unanchored persisted trade with no confirmed in-process cache, the original historical seed is unknown; this change cannot retroactively certify or recover that earlier calculation.

Session-open evidence comes from validated source candles or a known same-day registered trade. A weekday clock alone does not prove an exchange session is open: holidays/market closure must not cause fabricated outage alerts. If every provider is unavailable on cold start and no session-open evidence exists, status remains unknown and is logged only.

During a premium-data gap, pending index source observations are retained durably for chronological reconciliation. An observed index guard exit can be marked `AWAITING_OPTION_BACKFILL`; suppress active-reference cards while the earlier premium timeline remains unresolved. Backfill determines which protection event occurred first and preserves the actual milestone score. A recovered exit replaces a stale monitoring-restored/open card.

Validate continuity from the trade's durable `last_processed_option_bar_end` during common history preparation, before the candidate history can replace the confirmed cache, publish `_option_available_until` or populate the replay timeline. A truncated series that skips a same-session bar after that watermark remains unavailable even when it contains later completed bars. Both the combined market-event path and premium-then-index polling must defer post-T1 index observations until the missing premium sequence is recovered. An earlier recovered option close-through closes the remaining 50% with score 1 before a later T2 touch; if that close does not breach protection, the queued T2 can then advance normally. The existing contiguous-candle rules still accept adjacent bars and their supported overnight session transition.

`apply_option_lifecycle_transition` must atomically commit revision, canonical option context, active state, analytics score/result and immutable alert snapshots. Never fall back to separate REST writes. Persisted score 1/2 while a runner remains OPEN must not become a final ML training label. A terminal event retains the highest milestone; weighted index model P&L uses 50% T1, additional 30% T2 when reached, and the remainder's observed contemporaneous index mark, or remains unavailable when a required mark is missing. Option stop/close stays in premium fields; no inferred index exit price or broker fill.

The offline training entrypoint must carry the fetched canonical `option_trailing`, `result_state` and `trade_pnl` metadata into each matching snapshot row alongside entry/exit timestamps and time-metric exclusions. Canonical `trade_pnl` comes from `trade_analytics.pnl_points`; the dataset pipeline later exposes `pnl_points`. A terminal versioned option trade with no remaining allocation is eligible for the existing milestone-based classifier even if `ml_collection.trade_outcome` is missing. An open runner remains ineligible when snapshot telemetry incorrectly reports a completed target. Preserve unknown canonical P&L as null and retain existing unversioned legacy labeling behavior; joining metadata does not retrain, promote or deploy a model by itself.

Completed-candle option monitoring must propagate an initial index `SL_HIT` event to the existing signal-engine cooldown reset. Every production path that invokes that monitor supplies the engine, including normal candle fetches, session finalization, tick waits and fallback polling; index-provider failure must not discard an independently detected protective exit. This preserves the existing re-entry behavior after an initial stop-out. T1, T2, break-even, ATR, option-floor and empty event results do not introduce additional cooldown-reset triggers.

Revision recovery treats milestone progress and stop strength independently. When a queued T2 observation advances beyond the persisted T1 milestone but carries a lower stop, retain the T2 observation and rebase it onto the stronger persisted stop and its protection mode. The T2 transaction's context and immutable alert snapshot must agree on score 2, 80% model-booked allocation, 20% remaining, disabled index guard and the stronger stop. Keep the original event identity so retries cannot duplicate the T2 event; send the milestone alert only after its transaction succeeds. Later queued candles may already have raised the live stop above that T2 snapshot: replaying the earlier event must not lower live protection. For example, T2 can retain its rebased ₹150 stop while a later queued raise preserves the live ₹196 runner stop; a subsequent completed close at ₹180 exits against ₹196 with score 2 retained.

The same-phase conflict path must also retain pending index observations or an unresolved guard marker when the persisted context has a stronger option stop. Merge persisted and pending source observations in event-time order without duplicating identical observations, preserve the earliest guard marker, and carry the union into the live context while that phase still applies. Apply this union to every kept POST_T1 snapshot, including later stop raises and silent watermarks, so a subsequent queued write cannot erase the earlier committed guard. CLOSED and POST_T2 contexts have consumed their pending sources and must not have that journal restored. Keep the stronger stop and its protection mode. This repair preserves evidence awaiting backfill; it does not declare a guard exit or T2 milestone before chronological reconciliation. A restart must retain that evidence and active-reference alerts must stay suppressed while the guard remains unresolved.

When chronological reconciliation reaches T2 first, both T2 transition paths must explicitly set `observed_index_guard_exit` to `None` in Python and JSON null in the transaction payload. This includes an already-POST_T1 runner reaching T2 and one observation reaching T1 and then T2. Removing the key is insufficient: the existing RPC merges `v_old || new_context`, so omission retains an older `AWAITING_OPTION_BACKFILL` marker. Explicit null consumes that obsolete guard durably, survives reload and later context merges, and lets valid POST_T2 stop-raise alerts reach delivery after restart. If an older POST_T1 stop raise is rebased onto a committed POST_T2 context, copy the committed guard marker and pending-observation list together with the phase/allocation fields into the rebased payload and live state; a stronger stop must not resurrect evidence already consumed by T2. The trade remains score 2 with 20% running and the index guard disabled; unresolved POST_T1 evidence still follows the retention rules above.

Repairing an earlier queued stop raise must not overwrite a later observed CLOSED/STOPPED_OUT live state with POST_T2. Where the existing terminal-conflict rules allow a premium exit to retain a higher committed milestone, update its modeled `exit.exited_fraction` to `1 - booked_fraction` and keep that evidence consistent across the queued transaction, live context and immutable exit card. Thus a retained T2 score/booked allocation gives a 20% terminal remainder rather than the stale pre-T2 50%. An observed index break-even exit conflicting with a committed T2 still requires the existing reconciliation path; preserve its observed closed state while that write remains pending rather than accepting a new exit policy.

SQL execution belongs only to the approved backend role, with fixed schema resolution, revision/idempotency checks, terminal-state and monotonic protection validation. Existing open-trade migration must first produce a read-only preview with source evidence. Production migration, existing-trade correction, activation, deployment, restart and Discord replay remain separate authorized operations.

## 6. PR #147 correction acceptance matrix

| Review finding | Required regression evidence |
|---|---|
| R1 runtime wiring | Production entrypoint obtains pinned option candles, invokes new monitor, processes missed index/option observations chronologically, and retains the existing initial `SL_HIT` cooldown reset across monitor callers. |
| R2 activation gate | Defaults and missing/mismatched parity version preserve legacy behavior; explicitly signed-off test policy enables option lifecycle. |
| R3 revision ownership | T1 → T2 → repeated stop raises advance committed/live revisions; a stronger persisted stop cannot discard a newer T2 milestone or same-phase pending guard evidence; merged state/alert agree; event IDs remain unique; conflicts cannot claim success. |
| R4 atomic persistence | RPC failure never falls back to partial REST state; both tables agree after successful transition; explicit T2 guard null survives JSON merge/reload and does not suppress later runner alerts. |
| R5 durable Discord delivery | No recorded event sent before commit; restart restores pending snapshots; ack is durable; uncertain submission not blindly retried. |
| R6 T1 seed | Pre-T1 completed historical line initializes protection immediately; the next eligible close can exit against that seeded stop; exact warmup seed survives typed context serialization/restart and additional earlier history cannot shift it. |
| R7 candle ingestion | Native 5m and verified 1m aggregation are accepted correctly; forming, wrong-contract and malformed data cannot advance protection. |
| R8 contract metadata | Producer expiry_date/security ID/entry_limit pin the original contract and bracket; missing contract is never invented from strike. |
| R9 exit accounting | Option close uses premium fields; index exit mark is observed or unavailable; allocation-weighted model P&L preserves booked targets. |
| R10 SQL boundary | Lower-privilege role denied; backend only; revision and terminal/monotonic invariants exercised in isolated transaction tests. |
| R11 gaps/monitoring | Exact warmup seed and durable-watermark continuity are checked before cache, availability or replay publication; missing prefix or same-session gap freezes processing; recovery chronological; pending guard evidence survives stronger-stop conflicts/restart; one market-hours stale warning, no overnight outage spam. |
| R12 Cards A–L | Pure immutable snapshot rendering tests exact event time, contract expiry, units, allocation/exit evidence and all three transport outcomes. |
| R13 incident replay | Genuine #1950/#3935 contract metadata and source chronology; no inserted bars forcing desired closure; missing historical evidence disclosed. |
| R14 ML/recovery | Offline CLI preserves fetched canonical option outcome metadata; open runners excluded despite stale snapshot targets; terminal rows with missing snapshot outcomes label from the highest milestone; nullable P&L and unversioned legacy outcomes preserved. |

Report executed checks and measured results in the final delivery. Candidate arithmetic fixtures, synthetic lifecycle tests and real incident replays are separate evidence types. Current-day-only fixtures do not establish prior-session chart parity. No test result or production state is assumed from documentation.


## 7. Read-only carried-trade migration preview

Use an offline export; this utility never contacts Supabase, an exchange or Discord and has no apply option:

```bash
python scripts/preview_option_atr_migration.py /path/to/offline-export.json
```

The JSON input contains `active_trades` and `trade_analytics` arrays. Legacy open rows require a matching analytics row, known sourced timezone-aware registration timestamp (not merely the signal `entry_timestamp`), original index entry/SL/T1/T2, original bracket `options_sizing` contract identity and prices, and explicit sourced milestone history. Closed history is skipped; already-versioned trades remain unchanged. Current price never establishes a historical touch.

Optional `trade_analytics.market_context.option_atr_migration_evidence` declares:

```json
{
  "registered_at": "2026-10-23T09:16:00+05:30",
  "registration_source": "registration-audit-log-reference",
  "option_candles": [{"start_time": "2026-10-23T09:15:00+05:30", "end_time": "2026-10-23T09:20:00+05:30", "security_id": "51333", "complete": true, "open": 100, "high": 110, "low": 95, "close": 105}],
  "history_verified_through": "2026-10-23T09:20:00+05:30",
  "milestone_history_source": "source-export-reference",
  "milestone_events": [{"type": "T1_HIT", "event_time": "2026-10-23T09:17:00+05:30", "source": "index-source-observation-reference"}],
  "parity_signoff": {"policy_version": "option_atr_v1", "evidence_ref": "reviewed-chart-parity-report"},
  "schema_verification": {"migration": "20261023_option_lifecycle_transition.sql", "evidence_ref": "backend-role-and-function-verification"},
  "chronological_replay": {"policy_version": "option_atr_v1", "highest_milestone": 1, "terminal": false, "evidence_ref": "reviewed-replay-report"}
}
```

This shortened example deliberately lacks adequate continuous prior-session history and must remain ineligible. Supply actual completed candles including sufficient prior-session history; discontinuous, forming, duplicate or wrong-contract data remains ineligible. Evidence references are declarations requiring human verification; the script cannot certify that external evidence is true. A candidate still has `requires_approval=true`, `activation_allowed=false`, zero write operations and no historical alert replay. Stop reconstruction and any production correction require separate reviewed approval. Missing evidence is listed explicitly; no trade is automatically converted.

New-policy `activated_at` records local tracking registration (or the already confirmed paper fill), separately from the original signal source `entry_timestamp`. Completed source bars before activation are ignored; a bar straddling activation cannot supply earlier wicks to a newly armed guard. The fixed seed start and pending source/exit evidence survive typed context serialization.


## PR comment follow-up and scope decision

Watermark-only transactions persist revisions/source progress without inserting empty `alert_events` records. Only nonempty alert snapshots enter the delivery outbox. A separate server-owned `last_non_alert_event_id` stores one silent-transition retry identity; real alert commits preserve it, so lost responses can be retried under the existing serialized writer without adding watermark history to the outbox. This is not an unbounded deduplication ledger for arbitrary concurrent historical silent events.

The owner explicitly chose “Keep agreed exits; handle expiry separately” during review. This PR therefore adds no automatic expiry close, rollover, settlement valuation or new terminal score rule. The expiry finding is a real deferred boundary: a contract surviving its last available premium bar may remain OPEN. Its termination/valuation policy requires separate work; do not describe that boundary as fixed. Initial index SL, post-T1 index-entry protection, completed-premium ATR/floor exits and retained milestone scores remain the approved rules. Production activation is still disabled pending the existing release checks.

## Source-time replay ties

Buffered spot events and completed option bars are replayed in source-time order. A same-close tie is considered only after the trade's T1 activation and beyond its processed option-bar watermark. If a second-resolution spot event there touches T2 or the entry guard, persist an `ORDER_UNKNOWN` marker with the option close and active stop before skipping either source event. Do not infer T2-before-stop or close against the wrong allocation; reliable cross-market ordering evidence is required to resolve the accounting outcome.
