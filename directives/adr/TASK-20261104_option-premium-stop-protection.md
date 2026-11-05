# Architecture Decision Record — TASK-20261104 Option-Premium Stop Protection

- **Date:** 2026-11-04
- **Decision Status:** IMPLEMENTED AND VERIFIED LOCALLY; production rollout not authorized
- **Scope:** Lifecycle architecture and local implementation plan. No production configuration, deployment, worker start, broker order, or historical production-record change is authorized by this ADR.
- **Supersedes:** The post-T1 premium-stop rules in `directives/adr/TASK-20261023_option-atr-trailing-lifecycle.md` where they use the original option SL as the permanent floor or require a completed 5-minute close to detect a stop. Other previously approved lifecycle and accounting behavior remains unless explicitly changed below.
- **Clarification:** The user's 2026-11-04 follow-up requires every hit of the moved entry-cost stop to report the break-even status and `0.00` runner stop-out points, including a quote gap through cost. Keep the observed/reference exit mark separate; do not claim a fill at cost. This clarifies and supersedes the earlier below-cost-gap runner-points calculation in this ADR.

## 1. Problem Statement

The current option ATR path is enrolled only when the ATR feature/configuration and backend gates pass (`position_manager.py::register_trade`). It processes pinned option premium history after T1, but `PositionManager.update_option_atr_trailing` detects exits from completed 5-minute closes using a strict `close < active_stop` check. The separate selected-option quote path in `main.py::_record_selected_option_quote` serves the dashboard and is not a per-trade protection feed. Trades without the option ATR policy can still reach legacy spot ATR monitoring through `main.py::_monitor_completed_atr` and `PositionManager.update_atr_trailing`.

The required behavior is to protect the option premium before T1 with its initial premium SL; move the remaining premium reference/position to its entry premium at T1; and after T1 allow only a valid option-premium ATR stop above that protected level to tighten it. Premium stop enforcement must run on fresh pinned-contract price observations without waiting for an ATR bar. Existing independent spot/index SL and post-T1 index break-even guards remain in force in their own price domain. Spot ATR must not substitute for option ATR or control the premium stop.

The 4 November records for #2455 and #4176 are zero-lot `SIGNAL_REFERENCE` records. The persisted terminal events are not evidence of broker fills or realized losses. The available screenshots and stored records do not establish the exact earlier intrabar premium breach time; replay must report the earliest evidence-supported trigger or a timing bound, not force a screenshot estimate.

## 2. Decisions

### 2.1 Keep the protection domains independent

Keep the existing spot/index stop guard before T1 and index-entry break-even guard after T1. Add separate option-premium SL protection for any trade with a valid pinned contract and premium bracket. Evaluate each guard only against its own price series. Either may independently trigger the existing lifecycle close; never compare, project, or substitute a spot level and a premium level.

Disable the legacy **spot ATR trailing** path for every trade enrolled in option-premium protection. Keep that path only for spot-only trades. Missing premium history or a disabled ATR gate must never route an option trade back to spot ATR.

### 2.2 Premium protection lifecycle

For a valid option contract and bracket, create versioned premium protection state independently of ATR eligibility:

| Phase | Active premium stop | ATR role |
|---|---|---|
| Before T1 | Initial option-premium SL | No exit or stop authority; history may warm up |
| At T1 | Move to entry premium basis, preserving any already tighter stop | No gap in protection; persist T1 and the premium stop atomically |
| After T1 | `max(previous effective premium stop, entry premium basis, valid ATR stop candidate)` | May tighten only when a valid candidate is strictly above the current effective stop |

For a long-premium CE or PE, a fresh valid option-price observation **at or below** the effective stop triggers the premium exit path, including equality and gaps. A higher ATR candidate never loosens a stop; a lower/equal/missing candidate has no effect. Do not apply a completed bar's new candidate to quotes before that bar's end time.

The proposed canonical policy identifier is `option_premium_protection_v2`, separate from the indicator formula identifier `option_atr_v1`. Keep existing `option_atr_enabled`, exact approved-version, service-role, and backend capability requirements for ATR calculation/ratcheting. The ATR component remains off by default and is not activated by this implementation. The baseline premium SL and T1 cost protection are not disabled merely because the ATR parity gate is closed.

### 2.3 ATR calculation contract

Keep the configured indicator timeframe at five minutes; the supplied one-minute chart does not change it. Retain the current option-premium formula in `atr_trailing.py::calculate_option_atr_bars`:

- True range from the premium OHLC series, including prior premium close.
- Wilder ATR period 5, seeded by the first five ranges.
- Candidate formula: `max(High[i-k] - 2.5 * ATR5[i-k])` for the rolling last 10 completed bars (`k = 0..9`).
- Continuous history across sessions; no overnight reset unless the existing configured policy says otherwise.
- Candidate updates only from validated, completed, contiguous, contract-pinned 5-minute option bars.

Only the ATR-derived stop price is compared to the active premium stop. Raw ATR volatility is not itself a stop. A candidate must be strictly above the current premium stop to become effective and change its source to ATR.

### 2.4 Entry basis, trigger, and fill semantics

Record the premium basis with its provenance:

- **Confirmed execution:** the authoritative weighted-average fill for the pinned contract, with fill identity/time where available.
- **Signal/reference tracking:** the recorded premium reference (`options_sizing.entry_limit` when that is the canonical stored entry). Label it as a reference; do not call it an executed fill.

Do not silently make the basis fee-adjusted. A stop breach is a trigger event, not proof of a fill at the stop price. Persist trigger source, trigger quote price/time, active stop/source, and exit reason separately from an estimated reference exit and any confirmed broker fill. For actual execution, do not mark the broker position closed until the execution path confirms the fill; represent pending/failed execution explicitly. Reference-only lifecycle closure may use the first qualifying reference observation, clearly labeled as modeled.

Terminal labeling is based on the active premium-stop source:

- If the still-effective premium stop is the entry/cost stop, use `STOPPED_OUT_AT_BE` and report **0.00 runner stop-out points**, including when the observed quote gaps below cost. Persist the observed/reference exit mark separately; do not fabricate a fill at cost. Previously booked reference legs remain intact, and observed-price accounting remains distinct from this stop-out points field.
- If an ATR-raised stop is active, use `ATR_SL_HIT`, while keeping trigger mark, effective stop, and fill/reference exit distinct.
- A pre-T1 initial premium-SL event remains `SL_HIT` with an explicit `stop_domain=OPTION_PREMIUM` (distinguished from an independent spot-domain `SL_HIT`).

### 2.5 T1/T2, accounting, and event order

T1 and T2 remain spot/index milestones. Preserve existing suggested sizing, model bookkeeping/partial-booking fractions, trade scoring, and reference accounting unchanged. T2 remains a reference milestone: it does not add broker execution or allocation semantics and does not assert an actual partial fill. Do not introduce a new T2-driven premium-stop change. Preserve the existing approved lifecycle's T2 bookkeeping and guard behavior except where the explicit independent spot/premium guard rules above require separation.

Merge source-timestamped spot observations, option-premium observations, and completed option bars in chronological order. At same-minute boundaries, do not assume T1 preceded a stop breach. Preserve unresolved event ordering as ambiguous until source timestamps or finer-grained data resolve it. Deduplicate events by durable event identity so retries/restarts cannot double-close or reopen a trade.

When an option-premium stop triggers while an earlier index T2/entry-guard candle is unresolved, persist the first protective premium event on that ambiguity record and route its paper execution intent without closing the reference lifecycle. Keep the trade in its active post-T1 state until certified ordered index evidence resolves the earlier candle. Then book T2 first and apply the premium exit to the remaining reference fraction if T2 was first; if the entry guard was first, finalize the break-even index exit. If the premium trigger itself predates the first ordered index touch, finalize the premium exit first. Preserve the original quote/bar provenance and do not re-use later observations to rewrite the first protective trigger.

Apply the same ordering protection when a meaningful index touch is queued because required option history is incomplete. A later premium-stop quote immediately creates the execution exit intent, but its reference exit is stored as `deferred_option_exit` and the trade remains in its active post-T1 lifecycle. On recovery, replay queued index observations and completed option bars in source-time order. Process only bars strictly before the retained premium-trigger timestamp; an earlier bar close through the then-active premium stop finalizes the reference exit at that bar and remaining fraction before later index milestones. Otherwise apply the retained premium trigger using the fraction still remaining at that event. Never use bars at or after the retained trigger as earlier evidence, and preserve the already-routed execution intent. Do not let a terminal premium event strand earlier queued index evidence.

If an earlier recovered option stop wins, remove the superseded `deferred_option_exit` and later queued index observations before capturing the terminal payload. Commit that cleanup in the original terminal transition so the durable snapshot matches the chosen exit. Do not submit another lifecycle transition to clear those fields after the trade is terminal.

Missing option history may delay reference ordering, but it must not delay an eligible index-entry guard exit for a confirmed paper execution that has booked T1 or has a T1 partial awaiting a bid. For ATR-enabled v2 trades, retain and route that protective execution intent before queuing the index observation for history reconciliation; keep reference closure and accounting deferred where chronology remains unknown. Preserve the first retained protective intent and its reason, require the matching protected paper fill and pinned contract, and retain activation-time, event-watermark, and reference-only safeguards. The intent covers the remaining paper execution units and remains pending until an observed executable premium bid confirms the exit.

After T2 is committed, reject any premium quote whose source time is at or before `t2_at` as out of order and mark quote monitoring unavailable. Do not apply the later POST_T2 allocation retroactively to a pre-T2 premium observation; a same-batch quote with an earlier source time must be replayed before T2.

For a same-source-time v2 premium stop/index T2 or entry-guard tie, whether detected from an accepted premium quote or a completed option bar, retain the reference ambiguity and persist and emit the premium protective trigger before milestone processing can commit T2. Equality with the active v2 premium stop is a breach, matching normal option-stop evaluation. A quote arriving in a later batch at or before an already committed `t2_at` remains rejected as delayed. For confirmed paper fills, persist an execution-route-pending marker with the trigger reason. Route each trade independently; a sync failure for one trade must not abort the batch. Retry pending routes on later monitor passes, including for already-terminal trades. After dispatcher sync succeeds, acknowledge through a dedicated metadata-only RPC that works for terminal trades, checks the pending reason, and does not advance lifecycle revision or modify the final result. Keep the marker pending unless both synchronization and acknowledgement persistence succeed.

When certified ordered ticks resolve a v2 T1/T2-entry ambiguity to an index break-even exit after T1, store the exit as `SPOT_INDEX` and persist the same route-pending marker used by direct guard exits. The monitor must drain it through the existing paper execution dispatcher even though the resolver does not return an event list; reference resolution and execution confirmation stay separate.

For v2 trades, keep premium-quote health and ATR-history health as separate monitoring causes. Healthy ATR history cannot clear a quote outage, and a fresh quote cannot clear an ATR-history outage. Emit a restored state only after all feeds required by the trade's ATR configuration have recovered; when ATR is disabled, quote health remains the only required monitoring feed.

## 3. Premium Quote Source and Failure Policy

Current evidence establishes that the option candle path can fetch candles for a pinned security ID (`main._monitor_completed_atr`) and that `price_fetcher.fetch_option_observation(contract)` exists in the selected-option quote flow. The latter currently services a selected dashboard contract, not every active trade. `quote_service` persists `OPTION:SELECTED`, so that global selected quote must not be treated as per-trade protection.

Extend monitoring to request/consume a timestamped premium observation for **each distinct active pinned contract**, keyed and validated by `security_id`, exchange segment, expiry, strike, and option type. Prefer the Dhan live quote/feed subscription if it supplies per-contract exchange-time updates; otherwise poll `fetch_option_observation` for each distinct active contract at the monitor cadence. Feed these observations into the central lifecycle event ordering path, not just the dashboard cache. Document the actual update cadence and source timestamp; do not claim tick-level latency if the implemented provider is a polling snapshot.

Reject wrong-contract, malformed, stale, out-of-order, and future-dated data according to the verified feed contract. Missing/stale data leaves the last persisted effective stop unchanged, marks protection monitoring unavailable, and emits an operational warning. It must not lower/clear the stop or fall back to spot ATR. The system must not fabricate an option price. Quote outages are a real monitoring gap and must be visible.

## 4. Canonical State and Atomic Persistence

Persist a versioned protection object in canonical trade lifecycle context. The proposed shape is illustrative; implementation must preserve unrelated `market_context` fields:

```json
{
  "policy_version": "option_premium_protection_v2",
  "revision": 1,
  "contract": {
    "security_id": "39961",
    "exchange_segment": "NSE_FNO",
    "expiry": "2026-11-09",
    "strike": 23950,
    "option_type": "CE"
  },
  "phase": "PRE_T1",
  "entry_premium_basis": {
    "price": 167.20,
    "source": "REFERENCE_ENTRY_LIMIT",
    "event_at": "..."
  },
  "initial_premium_sl": 161.05,
  "effective_premium_sl": 161.05,
  "premium_stop_source": "INITIAL_OPTION_SL",
  "spot_guard": {
    "initial_stop": 23896.20,
    "post_t1_entry_stop": 23908.20,
    "enabled": true
  },
  "atr": {
    "enabled": false,
    "formula_version": "option_atr_v1",
    "atr_period": 5,
    "hhv_period": 10,
    "multiplier": 2.5,
    "last_processed_bar_end": null
  },
  "last_option_quote": null,
  "monitoring_status": "ACTIVE",
  "exit": null
}
```

Use the existing `apply_option_lifecycle_transition` transactional persistence path as the atomic boundary. Extend its policy/version validation and payload contract for the new protection state and terminal labels as needed; retain row locking, expected-revision checks, event-id idempotency, atomic lifecycle/analytics updates, and the durable alert outbox. Do not replace a failed atomic write with independent REST updates. Persist/recover the current stop, entry-basis provenance, contract identity, T1 state, ATR watermark/seed, quote watermark, exit trigger, and pending execution status before resuming monitoring after restart. Terminal v2 records with an unacknowledged route marker must also restore into a separate retry queue with enough persisted paper execution state to route the matching contract after process restart; missing or mismatched bindings remain pending and are never treated as live orders. Keep a terminal paper execution in that queue while its exit is `EXIT_PENDING`, even after the lifecycle route marker clears, until an observed premium bid closes the remaining units.

Recovery accepts lifecycle execution status `EXIT_TRIGGERED_PENDING_CONFIRMATION` as well as `CONFIRMED_PAPER_FILL`: a persisted premium trigger advances the lifecycle status without invalidating the protected paper fill. Both full snapshots and legacy contexts must still pass the existing PAPER mode, fill quantity, protection, and pinned-contract checks. Mark reconstructed bindings as exit-only and skip them in pending-entry deduplication; this avoids reading unavailable entry-signal fields and allows new signals to dispatch while the recovered exit waits for a bid. Ordinary pending-entry deduplication and the recovered exit intent retain their existing behavior.

Any SQL changes belong in a new idempotent migration with a preview for open records and local SQL integration coverage. Do not auto-enroll or reinterpret existing/closed production rows, apply the migration to production, or change runtime settings under this task.

## 5. Component Boundaries

| File/path | Responsibility |
|---|---|
| `position_manager.py::register_trade` | Initialize versioned premium protection for valid pinned contracts/brackets independent of ATR eligibility; retain current spot guard and reference accounting. |
| `position_manager.py::update_option_market_events` | Validate and chronologically merge per-contract premium observations with spot milestones/guards and completed premium bars; enforce active premium stop without waiting for a 5-minute close. |
| `position_manager.py::update_option_atr_trailing` and `atr_trailing.py::calculate_option_atr_bars` | Calculate eligible completed 5-minute option-premium candidates and ratchet only upward after T1; never exit from spot ATR or from a newly raised candidate retroactively. |
| `main.py::_monitor_completed_atr` | Fetch pinned option candles and fresh observations for each open protected contract, then invoke the canonical lifecycle path. |
| `main.py::_record_selected_option_quote`, `quote_service.py` | Keep selected-option dashboard persistence separate from lifecycle protection. Reuse provider normalization only; do not use `OPTION:SELECTED` as the stop source. |
| `storage.py::apply_option_lifecycle_transition`, `migrations/` | Atomically persist v2 state, terminal events, analytics, and alert outbox with revision/idempotency safeguards. |
| `tests/` and `tests/integration/` | Verify public lifecycle behavior, quote/bar ordering, restart/persistence, SQL transition, reference versus confirmed fill semantics, and configuration gates. |

## 6. API and Event Contracts

Each protected quote observation must include a validated pinned contract identity, premium price, source/exchange timestamp, and local received timestamp. The lifecycle evaluator must reject an observation that does not match the trade's frozen contract or is not newer than its durable quote watermark.

The lifecycle input path must accept these independent source types:

1. Spot/index candles and ticks for existing spot SL/BE and T1/T2 milestone decisions.
2. Per-contract premium quote observations for immediate premium-stop checks.
3. Completed, validated 5-minute premium bars for ATR state/candidate updates.

The output event must identify the trigger domain (`SPOT_INDEX` or `OPTION_PREMIUM`), event/source time, quote/bar price, active/effective stop and stop source, status/reason, reference/estimated exit where relevant, and confirmed fill only when execution evidence exists. Alerts must distinguish entry premium, initial premium SL, ATR candidate, effective premium SL, source quote timestamp, trigger, and fill/reference exit. A spot-entry stop must never be labeled premium break-even.

## 7. Definition of Done / Regression Evidence

Implementation and QA must exercise the actual lifecycle entry points and persistence/restart path, with external order execution disabled:

- Initial premium SL is active before T1; ATR cannot control a pre-T1 exit.
- T1 atomically preserves existing spot guard transition and moves the premium stop to entry cost without an unprotected interval.
- Premium BE is retained when ATR candidate is missing, below, or equal; a strictly higher valid candidate tightens the stop.
- A falling candidate, data refresh, restart, or stale/missing history never lowers/clears a previously protected premium stop.
- A valid fresh quote at, below, or gapping through the premium stop triggers between 5-minute ATR bars; trigger price/time are not presented as a fill.
- A premium quote at a wrong contract, with invalid/future/stale time, or out of order cannot mutate the lifecycle.
- A premium quote tied exactly to T1 at or below the original premium SL preserves order/allocation ambiguity and immediately creates a protective execution intent; advancing the quote watermark cannot discard the breach.
- Later quotes or ATR bars cannot finalize or rewrite that reference exit while the T1-tied protective trigger remains unresolved.
- A same-time T1 premium quote above the original SL but at/below entry cost remains order-unknown when an index tick also reaches T2. Preserve the quote and tied index observation before milestone processing; do not commit T2 or its reference allocation.
- Certified ordered spot/index ticks establish index-touch order only. If a T1-tied premium quote or bar remains unresolved, do not finalize a later index guard/T2 outcome or commit an assumed exit allocation from those ticks; retain the lifecycle ambiguity.
- Expired or malformed restored pins are isolated while building the protective quote batch; valid contracts continue to receive quotes, and no request is made when every pin is invalid.
- A contemporaneous spot mark recovered for a premium-triggered exit is persisted as `exit_price` in both lifecycle and accounting ledgers; absent evidence remains null.
- Bought CE and bought PE use identical long-premium `price <= stop` behavior.
- Existing spot initial SL and index break-even guards remain independent; pre-T1 premium breach uses `SL_HIT` with `stop_domain=OPTION_PREMIUM`; no spot ATR fallback runs for a protected option trade.
- T1/stop same-minute chronology is resolved from source timestamps or explicitly remains ambiguous; no lookahead/replay of prior ticks.
- Restart restores contract identity, premium basis/source, T1, effective premium stop/source, ATR warmup/watermark, quote watermark, and pending/terminal event state.
- Duplicate/concurrent events cannot duplicate a closure or reopen a closed lifecycle.
- Entry-cost stop trigger produces `STOPPED_OUT_AT_BE` and 0.00 runner stop-out points, including below-cost gaps; a gap records the observed/reference exit separately. An ATR-raised stop produces `ATR_SL_HIT`.
- Reference runners remain reference/model accounting; no test or report describes a model mark as a broker fill or realized P&L.
- Existing T2 reference accounting, strategy, sizing, and partial-booking behavior are unchanged.
- ATR feature gates remain disabled/default-off; baseline premium protection still functions without ATR eligibility.
- For #2455 and #4176, report durable IDs, contract, reference/fill mode, T1 time, stop history, quote/bar source times, earliest evidence-supported breach or timing bounds, old-path behavior, and corrected-path behavior. Do not infer an exact breach time from unavailable ticks or claim a broker fill.

Run focused unit and integration/replay/restart verification after implementation and report any unavailable live-provider or runtime evidence explicitly. This ADR itself makes no test or runtime claim.

## 8. Alternatives Considered

1. **Keep 5-minute close as the only stop trigger.** Rejected because it leaves a known gap between active premium protection and the next ATR candle close.
2. **Use the selected dashboard quote.** Rejected because it is a single mutable selection, not a feed keyed to every open trade's pinned contract.
3. **Replace spot SL/BE with premium-only guards.** Rejected because the request mandates option-domain ATR/premium protection but does not authorize removal of independent existing index-domain guards; preserving them avoids an unrelated strategy change.
4. **Use spot ATR when option data is unavailable.** Rejected because the price units/domain differ and would silently change the stop rule. Keep the last protected stop and report monitoring unavailable instead.

## 9. Risks and Rollout Boundaries

- A polled quote has bounded but nonzero latency; the implementation must expose the actual feed and polling cadence. If a timely per-contract quote source is unavailable, the monitoring gap must be surfaced and must not be described as immediate tick-level enforcement.
- The ATR formula's exact chart/feed parity remains subject to the existing approval gate. This ADR does not authorize enabling `option_atr_enabled`, changing approved/parity versions, deploying, restarting workers, or activating the feature in production.
- Reference accounting is not broker execution. Trigger, modeled/reference exit, and confirmed fill remain distinct data facts.
- Historical production records remain unchanged. Any existing-trade correction or production migration requires its own reviewed dry run and separate authorization.
