# TASK-210: Jev forward prediction POC assessment

**Date:** 2026-10-02
**Status:** Implemented POC; review fixes verified locally

## Recommendation

Use Jev as a second forward predictor in the **existing ARES Fly app**, on a separate process-group Machine. When any of the four ARES trade setups fires, consume the live signal-time data ARES already writes to `ml_collection` and `ares_signals`; do not fetch the market again. Allow at most one automatic Jev dispatch for an eligible live signal, persist a successful result, and post a follow-up while the signal remains fresh. Jev code lives in its own `system_one/` package, with no application-code imports in either direction between it and XGBoost.

Jev performs **inference only**. This POC has no Jev training, fitting, retraining, or model artifact. Store live Jev predictions alongside the existing ML prediction records in Supabase for evaluation and future offline training of an ARES model or ensemble. Historical labels measure whether Jev's probabilities are reliable and can supervise that downstream model; they do not train the hosted Jev model in this POC.

This replaces the earlier proposal for a second Fly app and independent Dhan collector. The user's latest instruction is to run alongside the main app and reuse the ML data. A separate process group fits that requirement while giving the trading process its own 768 MB Machine. [Fly process-group documentation](https://fly.io/docs/launch/processes/).

## What the repository provides

The four live setup types are `FAILED_BREAKOUT`, `OI_WALL_REJECTION`, `EXHAUSTION_REVERSAL`, and `TREND_CONTINUATION` (`models.SetupType`). The engine evaluates them from the latest closed one-minute candle, option chain/ATM data, IV change, and structural levels, then returns a signal with entry, targets, stop, and reasons. The Jev POC runs only for a fired signal of one of these types; it is not a continuous every-bar forecaster.

| Existing record | Signal-time content | Role for Jev |
| --- | --- | --- |
| `ml_collection` signal row | Feature groups for candle, volume, IV, OI, Greeks, structure, and meta; raw candle/ATM OI; wall context; `signal_uuid`; created after the signal path | Primary shared market snapshot, derived from the same live candle, chain, levels, and histories used for ML. |
| `ares_signals` | UUID, setup, direction, entry, T1/T2/SL, reasons, candle timestamp, database creation time | Barrier geometry and detector narrative. |
| `ml_predictions` event row | XGBoost's model-input `feature_snapshot`, version, probability, and canonical UUID in `signal_id` | Optional paired comparison. Its insert is asynchronous, so waiting for it would delay or suppress Jev when the XGBoost logger is unavailable. |

`main.py` computes XGBoost from the current candle, option chain, levels, and MLCollector histories; later in the same signal cycle it awaits a signal-bound `MLCollector.snapshot()`. The `ml_collection` row is therefore the best independent handoff. It is **the same market-data cycle**, although its engineered feature dictionary is not guaranteed byte-for-byte identical to `ml_predictions.feature_snapshot`; record both versions when comparing forecasts. Neither `ares_signals` alone nor a second Dhan fetch has the full feature set. [Main signal path](../../main.py), [ML collector](../../ml_signal/collector.py), [ML predictor](../../ml_signal/predictor.py).

The live Supabase project observed in the prior assessment is in bridge mode: `ares_signals.id` is bigint and `signal_uuid` is unique UUID. `ml_collection.signal_uuid` is populated for signal rows; `ml_predictions.signal_id` stores the UUID as text. The future TASK-150 UUID cutover needs an explicit consumer-query adjustment. The PR includes the initial `2026-10-01-task210-llm-predictions.sql` migration; this review has not applied it to a live database.

## Implemented flow and timing

```mermaid
sequenceDiagram
  participant A as ARES app process
  participant DB as Supabase
  participant J as Jev process group
  participant T as TypeSafe Jev
  participant D as Discord
  A->>DB: Existing signal and ML snapshot writes
  A->>D: Existing immediate alert
  J->>DB: Load persisted rollout cutoff
  J->>DB: Poll fresh post-rollout ML rows (~1 s interval)
  DB-->>J: ML snapshot + linked signal
  J->>DB: Atomic claim, then owned invocation marker
  DB-->>J: Confirm ownership and dispatch eligibility
  J->>T: One compact state, batched typed questions
  T-->>J: Probabilities and regime judgment
  J->>DB: Atomically archive result and complete invocation
  J->>DB: Atomically mark SENDING with event-time freshness check
  DB-->>J: Acknowledge delivery attempt ownership
  J->>D: Fresh separate follow-up, wait=true, no hidden retries
  D-->>J: Confirmed message or uncertain outcome
  J->>DB: Persist SENT / DELIVERY_UNKNOWN; never replay uncertainty
```

The worker has no Dhan calls, avoiding the rate contention created by the earlier collector proposal. It still shares Supabase reads, Fly app configuration, image, and app-level secrets; resource isolation applies to the Machines, not to those shared services. Fly's process groups are configured in one `fly.toml`, and each group runs on its own Machine. A deploy would create the Jev Machine after the configuration is merged, so deployment is a separate reviewed step. [Fly process-group documentation](https://fly.io/docs/launch/processes/).

Jev's model response may be fast, but total latency includes the existing live snapshot write, polling interval, database read, context building, API round trip, prediction insert, and Discord send. Start with a one-second poll and measure p50/p95 signal-to-prediction and signal-to-alert times for all four setup types. Do not claim a fixed ~200 ms result before a real account call. If polling dominates, evaluate an event-driven handoff later.

## Rollout cutoff, ownership, and restart behavior

Before enabling the live consumer, bootstrap its `llm_consumer_state` row once with `live_from` taken from the database clock. Use an atomic insert that leaves an existing row unchanged; every worker start must load that row and stop if it is absent. Keep the same consumer identity and cutoff across restarts, deploys, and model changes.

Use the pinned signal-bound `ml_collection.timestamp` as `event_at`. In the current path, `main.py` captures `now` at market-cycle start, before fetching/detection and the awaited `process_options_calculation()`, then passes that unchanged value to the collector. The persisted normalized UTC time is a conservative lower bound on detection; it is neither the insert-generated `ares_signals.created_at` nor the candle-derived `ares_signals.timestamp`. This uses an existing event timestamp without modifying or importing the main/ML modules. [Cycle timestamp](../../main.py), [Signal snapshot timestamp](../../ml_signal/collector.py), [Signal persistence](../../storage.py).

For the initial POC, pin `expires_at = event_at + 60 seconds` in the job, and require `live_from <= event_at <= database_now < expires_at` for invocation and each allowed delivery attempt, within the active trading session. At the exact expiry boundary the signal is stale. Reject missing, invalid, future-dated, or unverified event timestamps without falling back to insert/receipt time. Neither late inserts nor retries/restarts may change event time or expiry. A cycle before rollout remains excluded even if its signal is inserted afterward. A signal delayed more than 60 seconds before insertion receives zero Jev calls/alerts even when its `created_at` is new. Measure eligible-signal coverage: the conservative cycle-start clock includes fetch time and may suppress more signals than an exact detection clock, which would need a separately reviewed capture change.

Persist one `llm_prediction_jobs` row per signal UUID. A database transaction must enforce claim ownership before any external request: atomically insert/claim the job with an owner token, lease expiry, pinned snapshot identity, model, and context/question versions. An owned, unexpired `CLAIMED` job can transition to `INVOKING` only once, setting `invocation_started_at`, while rechecking cutoff, age, and session eligibility. The invocation marker pins a dispatch deadline to the earliest event expiry, lease expiry, or IST session close. The consumer conservatively subtracts the whole RPC round trip from that database-derived budget and checks it again after SDK setup. A worker can dispatch only after this transition is acknowledged; an uncertain database acknowledgment is not permission to send. Losing workers and expired owners cannot dispatch. Result persistence must verify the matching invocation token.

| Durable state | Recovery action |
| --- | --- |
| `CLAIMED`, no invocation marker, lease expired | Atomically transfer ownership to a new token only if the signal is still fresh. The old owner can no longer transition to `INVOKING`. |
| `INVOKING`, worker lost or request outcome uncertain | Mark `UNKNOWN` after the bounded request/lease deadline; never automatically re-invoke. |
| `UNKNOWN` | Retain the original invocation identity for audit or reconciliation. A late valid response with that token may complete the result; it cannot cause a second dispatch. |
| `COMPLETED` | Reuse the persisted result; consult its separate durable alert state. Only `PENDING`/`RETRYABLE` may obtain a new acknowledged sending marker after a fresh event-time check; never replay an uncertain delivery or invoke Jev again. |
| `FAILED` / `EXPIRED` | Retain the reason, issue no automatic Jev replay, and suppress live alerts. |

Recheck event age immediately before Jev dispatch and immediately before **every permitted Discord delivery attempt**, including the first send and proven-non-delivery retries after backoff or restart. Re-read eligibility with database time after waiting; do not reuse the first attempt's check or rely on the worker's clock. If the check fails or the database is unavailable, do not send. Once expiry or session close is reached, persist terminal `alert_status = SUPPRESSED_EXPIRED` for pending/retryable deliveries, retaining the successful prediction/inference status. Already attempted uncertain deliveries retain `DELIVERY_UNKNOWN` with expiry recorded separately, since absence of delivery is unproven. A valid Jev response arriving after expiry may be stored with alert suppression. Historical evaluations require a separate explicit run with live Discord disabled; they do not change the live cutoff or job ledger.

Disable delivery SDK/transport retries that bypass the application gate. Record each attempt's start time and bound its request duration by the remaining freshness window. The contract prevents starting stale requests; a request already accepted by Discord cannot be recalled if its acknowledgment arrives after expiry. A documented non-delivery rejection at event age 59 seconds followed by a proposed retry at age 60 seconds must result in no second send, whether or not the worker restarted in between.

## Discord delivery ownership and uncertain outcomes

Keep alert delivery state separate from the successful inference in `llm_prediction_jobs`. Result completion initializes `PENDING`; before each webhook call, an atomic eligible `PENDING`/`RETRYABLE` to `SENDING` transition pins payload/destination and records an attempt token, owner, and start time. Freshness/session checks apply in that transition and again immediately before transport. Dispatch only after its database acknowledgment; an uncertain acknowledgment means no send. Only that attempt's owner can submit the request and record its result, and an expired lease never permits another owner to send a `SENDING` attempt.

Use `wait=true` to obtain the created message and persist `SENT` with its message ID under the matching attempt token. Retry a failed database write using the original acknowledgment, without repeating the webhook. A late matching acknowledgment may reconcile the original send; it never authorizes a new one. Discord documents confirmation via `wait`, but the reviewed webhook API supplies no assumed request idempotency mechanism. [Execute Webhook](https://docs.discord.com/developers/resources/webhook#execute-webhook).

Treat timeouts, response loss, ambiguous server errors, and crashes after the sending marker as uncertain. Once the attempt's bounded deadline elapses, an abandoned `SENDING` becomes terminal `DELIVERY_UNKNOWN` with no automatic resend. A crash before transport can therefore lose the alert too: restart cannot prove that the marked attempt never sent. This is a deliberate trade-off against duplicate follow-ups, not exactly-once delivery. The result remains available for audit and future training regardless of delivery status.

Only a documented rejection proving no message creation (for example, an explicit rate-limit response) may transition the matching attempt to `RETRYABLE` with recorded backoff. A permanent rejection is `DELIVERY_FAILED`; a generic network/5xx failure is not proof of non-delivery. After backoff or restart, any retry must atomically acquire a new marker and pass a new event-time freshness check. `SENT`, `DELIVERY_UNKNOWN`, `DELIVERY_FAILED`, and `SUPPRESSED_EXPIRED` do not authorize another send. A future retry/idempotency policy requires explicit review of provider guarantees.

This is an **at-most-once automatic dispatch policy**, not an exactly-once successful inference guarantee. Persisting a unique prediction UUID after the request does not prevent duplicate calls. Disable application, SDK, and transport inference retries: TypeSafe documents `RetryPolicy(max_retries=0)`. The reviewed API documentation does not establish a provider idempotency guarantee, so a timeout or crash after the invocation marker must not be replayed merely with a UUID header. This deliberately favors avoiding duplicate requests over filling every result; any future replay policy requires explicit review of provider guarantees. [SDK retries](https://docs.typesafe.ai/sdk/python/api/retries), [HTTP API](https://docs.typesafe.ai/api).

## Exact context and Jev question contract

Use Python to validate bullish/bearish barrier order, finite prices, and positive SL distance. Compute T1/T2/SL distances and reward-to-risk. Read runway, wall, IV trend, PCR/OI, volume, and wick values from the persisted feature groups and raw fields. Mark absent or stale values as missing, including structural levels or option-chain details that were not saved. Do not recreate missing facts from `reasons` text or silently replace null with zero.

Structural runway uses the collector's actual `dist_to_nearest_resistance`, `dist_to_nearest_support`, `dist_to_pdh`, and `dist_to_pdl` fields. Python reconstructs saved level prices from `ml_collection.spot` (nearest-resistance/PDH add their distance; nearest-support/PDL subtract it), selects the nearest saved level at or ahead of entry in the trade direction, and computes `runway_margin_beyond_t1_pts = nearest_distance - t1_distance`. `path_to_t1_clear` is true only for a strictly positive margin; a level at T1 counts as friction. Missing spot or no usable saved forward level remains unknown, rather than claiming unlimited clearance. This does not assume unsaved level counts or a complete inventory of all barriers.

Freeze `signal.entry_price` from `ares_signals.spot_at_signal`, the same spot passed to `PositionManager.add_trade`; preserve the detector's `trigger_price` separately. Include `signal.original_stop_loss` and `signal.post_t1_stop_price = signal.entry_price` in the state. In production, T1 changes the stop to `entry_price` for both directions, so the conditional T2 event must use this breakeven barrier. [Trade entry and trailing-stop behavior](../../position_manager.py).

The implemented `build_questions()` batches seven independent questions in one call. They see the same prepared state and do not consume each other's answers. The authoritative instructions and criteria are in [system_one/jev.py](../../system_one/jev.py).

| Question ID | Primitive | Mapping |
| --- | --- | --- |
| `first_barrier` | Choice: T1 first, original SL first, neither by close | T1 and SL probabilities from one distribution |
| `t2_given_t1` | Choice: T2 hits, T2 fails, insufficient evidence | Python multiplies T1-first probability by `t2_hits`; the post-T1 stop is entry/breakeven |
| `market_regime` | Choice: trending, range/choppy, volatile event, insufficient evidence | Label, distribution, confidence |
| `price_action_strength` | Score: five ordinal levels | Scale to 0–10; 40% of quality |
| `structural_clarity` | Score: five ordinal levels | Scale to 0–10; 40% of quality |
| `confluence_rating` | Score: five ordinal levels | Scale to 0–10; 20% of quality |
| `is_trap` | Noul | False-break/stop-sweep probability |

`setup_quality = 0.4 × price_action + 0.4 × structure + 0.2 × confluence`, after Python scales each score to 0–10. A displayed quality such as 7.5 is this composite, not a separate probability. Trap risk is Jev's Noul judgment; regime is its Choice judgment over the supplied market evidence. These are shadow estimates awaiting outcome validation.

The worker uses `TypeSafeClient(model=DEFAULT_MODEL, retry=RetryPolicy(max_retries=0), timeout=DEFAULT_TIMEOUT)` after the acknowledged invocation gate. Defaults are `jev-1.13.0` and five seconds. It stores the resolved model name, all seven typed answers, context version (`v1.2`) and question version (`v1.1`), and prepared input state. A conditional T2 `insufficient_evidence` distribution remains visible in the raw archive. No live account call was made during this review. [TypeSafe API](https://docs.typesafe.ai/api), [Python SDK](https://docs.typesafe.ai/sdk/python).

The evaluation labels must use the same stop transition: T1 success means T1 before the original SL; T2 success means T1 followed by T2 before a return to entry or session close. For a bullish entry at 24,000, T1 at 24,050, and T2 at 24,100, the path 24,050 → 24,000 → 24,100 is T1 success and T2 failure because the runner exits at breakeven. For a bearish entry at 24,000, T1 at 23,950, and T2 at 23,900, the path 23,950 → 24,000 → 23,900 has the same labels. Once breakeven is touched, later price movements cannot change that T2 failure.

The API's typed probabilities are **not yet calibrated ARES trade probabilities**. Compare them with realized outcomes, base rates, and XGBoost using Brier score and reliability plots before interpreting percentages as calibrated. Bars with unknown ordering of T1/original-SL, T1/return-to-entry, or post-T1 T2/breakeven touches must be labeled ambiguous and excluded from calibration. Production's optimistic candle evaluation may report a target in such a bar; retain that reported result separately from the ordered barrier label. The previous assessment's Laya CPU latency claim and the old Obsidian note's calibrated-example language were unsupported; Jev is the initial POC backend.

## Persistence and deployment contract

The migration creates `llm_predictions` with `signal_uuid uuid NOT NULL UNIQUE REFERENCES ares_signals(signal_uuid)` in the current bridge schema, three checked 0–1 probabilities, regime/distribution/confidence, quality/trap scores, resolved engine name, input/source versions, latency, raw state/response, and timestamps. Add `llm_consumer_state` for the persistent rollout cutoff/age policy and `llm_prediction_jobs` for unique signal claims, pinned input, owner token, lease, invocation marker, status, error, and alert-delivery state. Commit the result and job completion atomically; on a lost commit acknowledgment, read/retry persistence using the same token and response without rerunning Jev. Enable RLS and grant only server-side access to these tables and the atomic claim operation. Handle the later signal UUID column rename in queries. [Supabase API security guidance](https://supabase.com/docs/guides/api/securing-your-api).

The PR configures `[processes] app = "python main.py"` and `jev = "python -m system_one.consumer"` in the existing `fly.toml`, with a 256 MB Jev Machine and the 768 MB trading Machine. Apply the reviewed migration before enabling the worker. This review has not deployed Fly configuration or changed the live database. The migration is the initial unmerged schema, not an upgrade for an already deployed TASK-210 schema. Fly deploys process groups together from the same image; app-level secrets are shared, so the Jev process should never initialize or call Dhan. If separate secret isolation later becomes necessary, the one-app constraint would need reconsideration. [Fly process-group documentation](https://fly.io/docs/launch/processes/).

### Required deployment credentials

The Jev process needs `SUPABASE_URL`, backend-only `SUPABASE_SERVICE_ROLE_KEY`, `TYPESAFE_API_KEY`, and the main `DISCORD_WEBHOOK_URL`. README's Fly secret command, local `.env` sample, and `.env.example` now include them. The local worker launch exports `.env` into the shell because the worker reads environment variables directly. `SUPABASE_KEY` remains the trading process's existing credential; Jev does not fall back to it because it may be an anon key without access to the protected prediction tables/RPCs. Provision these secrets and the reviewed migration before enabling the worker. This review does not alter live secrets.

Definitive Jev HTTP 4xx rejections (except HTTP 408), including auth/validation/rate-limit failures, record inference `FAILED`. Local SDK configuration errors also record `FAILED`. Timeout/connection loss, HTTP 408, 5xx, unusable response, and unexpected post-dispatch failures remain `UNKNOWN`. Neither state is automatically re-invoked, and neither produces a prediction or Discord follow-up. Error type/reason remains in the job for operational audits. The live documentation index was reachable during this review but the exception-reference page was unavailable; exception hierarchy and status fields were verified against the installed SDK.

## Prediction archive and future training export

Both predictors retain their own tables in the same Supabase project: XGBoost writes `ml_predictions`, and the independent Jev worker writes `llm_predictions`. Pair them through the canonical signal UUID, using the current text UUID in `ml_predictions.signal_id` rather than the legacy bigint signal ID. A future read-only export can join these records with `ares_signals`, the pinned market snapshot, and ordered barrier outcomes. Use left joins, explicit missingness flags, and a documented model-version/source selection policy to prevent silent sample loss or duplicate training rows. The POC does not implement a training export or view. [Supabase joins](https://supabase.com/docs/guides/database/joins-and-nesting).

Persist every successful Jev result even if Discord delivery fails or is suppressed. Archive the exact input state, snapshot identity/source observation times, full typed response including `t2_given_t1`, resolved engine/context/question versions, signal timestamp, invocation start, response receipt, and database persistence time. `available_at` currently records database transaction time, not a proven commit-visibility timestamp. A future export must establish conservative commit/read availability before treating the result as a decision-time feature; asynchronous inserts must not make Jev or XGBoost appear available at an earlier signal timestamp. An XGBoost event's current `timestamp` alone does not establish database availability; a future export that uses it as a decision-time feature must establish an actual availability timestamp or mark it unknown and exclude it from that use.

Jev outputs can become candidate features for a future ARES model or ensemble; they are not outcome labels. Generate labels separately from the future spot path with the T1/original-SL and T2-before-breakeven definitions above, retaining label version and resolution time. Exclude ambiguous and unresolved labels from supervised examples. Keep the existing production trade result separate, since it may use optimistic same-bar ordering or a different horizon.

Use chronological splits with outcome-window purging and prospective or out-of-fold base-model predictions. Enforce feature availability at the downstream model's intended decision time. If Jev arrives after signal creation, it cannot be used to claim a signal-time decision; if a target/stop has already resolved, that response remains archived but is not an input for the resolved decision. A later decision horizon would need its own target/eligibility contract. These controls prevent future information from leaking into apparent training performance.

This POC collects the archive and required provenance. Training/export implementation and changes to the existing ML module remain future reviewed work; the live Jev consumer never waits for them.

## Verification before operational use

1. Mock TypeSafe, Supabase, and Discord at public boundaries; test exact context math, signal UUID joins, missing evidence, event mapping, probability invariants, retry/idempotency, and alert delivery. Include both bullish and bearish T1 → breakeven → later T2 paths as T2 failures, direct T1 → T2 paths as successes, and unordered competing barrier touches as ambiguous. Aim for the ticket's 100% new-unit coverage target.
2. Static check for zero Jev imports or changed lines in `main.py`, `SignalPredictor`, and `ml_signal`. Kill the Jev Machine and confirm the trading Machine and original alert continue.
3. With a server-side `TYPESAFE_API_KEY`, run representative saved signals and record actual model version, response time, token cost, coverage, and end-to-end latency. This key was unavailable during the assessment; no live Jev result is claimed.
4. Build forward barrier labels from signal-time spot data, compare Jev and XGBoost against the same event definition, and inspect calibration by setup type and market regime. Neither model should influence trading until that review.
5. Verify first rollout with historical signal rows and an empty result table produces zero historical calls/alerts; verify the cutoff survives restarts and model changes. Exercise fresh versus expired restart recovery and late responses without stale Discord delivery.
6. Test simultaneous claims against a real local database, then mocked provider timeouts/crashes before and after the invocation marker. Verify only the acknowledged owner may dispatch, pre-dispatch stale claims can be recovered, uncertain invocations are never replayed, and persistence retries do not call Jev again. The local SQL harness exercises ownership transitions serially; simultaneous multi-connection claim races remain an operational verification gate.
7. Verify event-time freshness with a market cycle delayed more than 60 seconds before insert, a pre-rollout cycle inserted afterward, missing/future/invalid timestamps, UTC normalization, and restart preserving the pinned expiry. Verify the Discord gate on every permitted attempt: a proven rejection may retry within the window, but a rejection at event age 59 seconds followed by a retry at age 60 seconds is suppressed across restart. Include session close during backoff, unavailable database checks, and disabled hidden transport retries.
8. Test atomic delivery ownership against a real local database and mock Discord acceptance with a lost response, crashes after the marker both before/after transport, an uncertain database acknowledgment, delayed success persistence, competing workers, and stale owners. Confirm ambiguous attempts become `DELIVERY_UNKNOWN` without a second webhook, while acknowledged persistence retries reuse the same message ID. Mocked transport tests and the local SQL lifecycle harness cover these transitions; real multi-connection races and live provider behavior remain unverified.
9. Verify the prediction archive retains successful responses when Discord fails or expires. For future export implementation, exercise missing model results, multiple model versions, UUID joins, unresolved/ambiguous outcomes, and delayed responses. Confirm prediction values never become labels, duplicate records do not multiply examples, and records unavailable at the intended decision time cannot become training features. These are planned verification gates, not executed training or database tests.

## PR review verification (2026-10-02)

- `python3 -m pytest tests/unit/test_task210_system_one.py -q`: **44 passed**. Regression cases include slow invocation acknowledgments, expiry during SDK setup, final-dispatch expiry, unavailable gates, lost send responses, ambiguous HTTP errors, success-ack persistence retries, atomic-result acknowledgment retries, and restart delivery from a saved prediction without another Jev call.
- `PGLITE_MODULE_URL=file:///path/to/pglite/dist/index.js node tests/integration/task210_lifecycle.mjs`: **37 PostgreSQL lifecycle assertions passed** using an isolated `@electric-sql/pglite@0.5.8` installation. The harness executes the actual migration locally with synthetic fixtures; no production data or network is used. Session boundaries are tested, then session eligibility is overridden for deterministic lifecycle cases. Queries run serially, so this does not prove multi-connection race behavior.
- `python3 -m pytest tests/ -q --import-mode=importlib`: **900 passed, 10 subtests passed**, with nine existing warnings. The ordinary pytest mode encounters an existing `tests.unit` import collision. `git diff --check` and an AST import-boundary check passed; the trading/ML files have no diff against the merged `origin/main`. Independent Debug/QA review confirmed the late acknowledgment/SDK setup gate and found no additional concrete bug.
- The review fixes preserve both branches' changelog entries when merging `origin/main`, gate claims/invocation/sending using database wall time, and atomically complete results without resetting existing alert delivery on a lost acknowledgment.
- That earlier review did not perform a live TypeSafe/Discord call, Supabase migration, Fly deployment, or calibration evaluation. Machine failure isolation, latency/cost, a deployed-schema upgrade, and multi-connection race testing remain rollout checks.

This is an implemented POC on the existing PR, using shared persisted market data and separate application code. The worker can use only fields saved by ARES; it cannot recover unsaved Dhan history or full option-chain details. Future training exports remain separate work.

## Additional PR review fixes (2026-10-02)

- Reproduced the runway and error-classification findings with failing tests before changing implementation.
- `python3 -m pytest tests/unit/test_task210_system_one.py -q`: **67 passed**. Added actual collector-output contract cases, bullish/bearish clearance, prior-day barriers, equality at T1, entry/snapshot offsets, missing evidence, explicit service-role credential behavior, and definite versus ambiguous SDK errors.
- `python3 -m pytest tests/ -q --import-mode=importlib`: **923 passed, 10 subtests passed**, with nine existing warnings. Independent Debug/QA review confirmed collector units/signs, SDK exception mapping, and zero ML imports; its local credential documentation finding was fixed. `git diff --check` and worker import-boundary checks passed. No schema, live credentials, main trading code, or ML application code changed in this follow-up.

## Latency and coverage review fixes (2026-10-02)

The snapshot metric now times the actual `ml_collection` batch query in the poller and is attached to each returned snapshot. Direct snapshot callers that did not measure a poll record null, not an invented zero or the signal-table read. Signal lookup has its own metric. Query time is shared by rows in one batch; it is not a separate query per row.

| Stored metric | Observed interval |
| --- | --- |
| `latency_snapshot_read_ms` | Monotonic duration of the actual snapshot query |
| `latency_signal_read_ms` | Monotonic duration of the linked signal lookup |
| `latency_context_build_ms` | Context preparation, including optional XGBoost comparison read |
| `latency_typesafe_ms` | Jev SDK call/response processing duration |
| `latency_persistence_ms` | Monotonic duration through acknowledged result/job persistence, including its retries |
| `latency_delivery_ms` | Latest delivery-processing attempt: gates, transport if attempted, and status acknowledgment; pinned once total is confirmed |
| `latency_signal_to_prediction_ms` | Database event age when archiving the result, before that transaction's commit acknowledgment |
| `latency_total_ms` | Database event age at metric finalization after acknowledged persistence and confirmed Discord delivery/status; null for unconfirmed delivery |

`record_llm_prediction_latency` computes total using the pinned event and database wall clock. It includes the upstream write/queue delay, result persistence, and delivery acknowledgment, plus metric-finalization request overhead. It also runs after restart delivery, preserving the initial persistence-stage measurement where available. It never changes job/delivery state or repeats an external request. A caller flag alone cannot fabricate success: the job must reference this persisted prediction and have durable `SENT`. The first confirmed sample is retained on later recording attempts. A failed finalization leaves missing data; metrics failure is logged and never authorizes resend. Compare p50/p95 only among confirmed samples and report missing/failed/suppressed/uncertain counts alongside them.

**Verification:** 202 focused tests, **595/595 statements and 156/156 branches covered (100%)**, no excluded lines or partial branches. The CI test job now enforces `--cov=system_one --cov-branch --cov-fail-under=100`. Full regression: **1,058 tests and 10 subtests passed**, with nine existing warnings. Actual local migration: **48 PostgreSQL lifecycle assertions passed**, including a 40-second pre-consumer event age, optimistic early-total rejection, confirmed/uncertain delivery, identity checks, sample pinning, and service-role restrictions. Coverage describes Python runtime execution, not instrumented SQL coverage or live provider correctness.

See [QA coverage report](../qa/TASK-210_coverage-report.md) and [machine-readable coverage summary](../qa/TASK-210_coverage-summary.json). The independent final QA audit was attempted but hit the agent usage limit; these are directly verified test results, not an independent QA approval. No live service, deployment, or production schema was changed.


## Jev lifecycle P1 review fix (2026-10-02)

The worker now waits 30 seconds between clock checks outside 09:15–15:30 IST instead of exiting after close. Recovery and database polling run only inside that window after startup loads the persisted rollout state. The same process resumes on the next session without resetting its cutoff. Fly restart policies are scoped to `app` (`never`) and `jev` (`always`), preserving the existing app-only cron and giving Jev an independent lifecycle in the existing Fly app.

The 256 MB Jev Machine incurs running charges overnight and on non-trading days. [Deployment instructions](../../DEPLOYMENT.md#4-scaling-lifecycle--scheduled-execution) explain process-group/policy checks, verifying that Jev is running after rollout, and explicitly starting it if previously stopped by an operator. No daily Jev cron is needed. Session eligibility retains the existing time-window behavior; this change does not introduce an exchange holiday calendar.

Five selected lifecycle regressions failed before the implementation change. The updated tests cover after-hours startup, both module entrypoints, and Friday close through the next business session with two different signal predictions and one bootstrap. Focused verification: **204 passed**, **592/592 statements and 154/154 branches covered (100%)**, no exclusions or partial branches. `fly config validate --strict --config fly.toml` passed. Independent read-only P1 QA/debug audit found no actionable bug and independently reran the 204 tests. See the refreshed [QA report](../qa/TASK-210_coverage-report.md) for full regression results. No deployment, external cron change, live provider call, or schema change was performed for this fix.


## Polling review fix (2026-10-03)

The worker pages past consumed snapshots before limiting the batch to ten eligible signals, so a burst of eleven signals cannot strand the oldest behind ten already-claimed rows. Each page is ordered by timestamp and snapshot UUID, deduplicated by signal UUID, and carries its own measured snapshot-query duration. A never-invoked claim remains eligible for the existing authoritative database claim/recovery gate. Freshness, rollout cutoff, invocation ownership, and delivery guards are unchanged. Tests/CI are not awaited for this push, as requested; earlier coverage numbers are historical evidence and do not certify this revision.


## Database expiry and UUID review fixes (2026-10-03)

`poll_jev_signals` replaces client history pagination with one bounded response. It uses database wall time and the persisted consumer policy, excluding expired, pre-rollout, future, and blocked-job rows before returning up to ten distinct signals. Only uninvoked, expired-lease claims matching consumer/snapshot/event/model/context may reappear; the atomic claim still rechecks ownership and freshness. An indexed recent-snapshot scan supports the time predicate. Snapshot latency now measures this actual polling RPC, separately from the canonical signal-read RPC.

Polling, signal reads, claim lookup and FK wiring resolve canonical UUID columns from the database catalog, preferring bridge `signal_uuid`, otherwise greenfield `ares_signals.id` / `ml_collection.signal_id`; numeric legacy IDs are never used. Dynamic identifier quoting and bound values keep query construction constrained. Resolution on each operation also supports a cutover after this migration was installed. New RPCs use SECURITY INVOKER and grant execution only to service_role. The worker remains independent of the main/ML Python modules.

Updated external-boundary fixtures and added `tests/integration/task210_polling.mjs` for expired history, the eleven-signal burst, persisted age policy, job recovery, permissions, both initial schema layouts, and cutover after installation. No tests or CI were awaited for this push, per user instruction. The prior coverage report is historical evidence, not a claim for this revision. The migration remains the initial unmerged schema; a previously applied version requires a separate reviewed upgrade. No live database or deployment was changed.


## Alert recovery backoff review fix (2026-10-03)

`poll_jev_alert_jobs` filters future backoff, stale/future/pre-rollout events, session closure, missing persisted prediction and non-pending delivery state using database time before limiting to ten jobs. Eligible work is ordered by earliest expiry then job ID; ten jobs waiting on backoff cannot hide fresh pending alerts. A partial consumer/expiry/ID index supports this selection. The RPC is SECURITY INVOKER and executable only by service_role. Selection remains advisory: the existing final atomic send marker rechecks freshness, backoff and ownership before every transport attempt. Saved predictions are reused without another Jev call.

Updated boundary fixtures and database contract cases cover backoff starvation, ordering, ready retry, expiry, session and permissions. No tests/CI were awaited for this push per the user's existing instruction; no new coverage claim, live migration or deployment was made.


## Retry clock and scheduling review fixes (2026-10-03)

Discord backoff persistence now sends a relative `retry_after` interval to `mark_jev_alert_retryable`. The RPC locks the matching SENDING attempt before reading database wall time, validates the interval, and atomically writes RETRYABLE/deadline/reason. Invalid intervals, obsolete tokens and already-transitioned attempts cannot update or renew the deadline. SECURITY INVOKER and service-role-only execution retain the existing delivery ownership boundary. Worker wall-clock offsets cannot schedule the retry early or late.

The main loop now polls/processes fresh signals before recovering saved alerts. Recovery handles only the first eligible saved job in database expiry/ID order per loop, then returns to polling; it cannot drain ten serial Discord requests ahead of a new signal poll. The existing per-request timeout/freshness and final send marker still apply. This is a scheduling bound on recovery attempts, not a throughput or end-to-end latency guarantee under database/provider stalls.

Added boundary cases for relative scheduling without reading the worker clock, single-job recovery, and fresh inference preceding slow recovery; extended SQL contracts for stale tokens, non-finite/negative intervals, database-relative deadlines, replay and RPC permissions. Syntax/diff checks only; tests/CI were not awaited per user instruction. No live migration, deployment, or new coverage claim was made.


## Fresh inference/webhook separation review fix (2026-10-03)

`process_signal` now stops after atomic prediction archive and PENDING delivery completion. It performs no webhook request. The main loop gives the selected snapshots their gated inference opportunities before processing one eligible saved prediction from the durable delivery queue. New alerts and retries share the same database-time expiry/backoff/ownership/send-marker rules; a slow webhook cannot delay inference for later snapshots in the current batch. Completion state survives worker restarts and no inference is repeated for delivery.

Persistence latency is observed immediately after archive acknowledgment; delivery and total remain null at that stage. Queue processing retains the persistence observation and finalizes delivery/total after the normal confirmation rules. Added ordering and queue-state boundary cases, including two inferences before a deliberately slow webhook. Syntax/diff checks passed; tests/CI were not awaited per the existing user instruction. No live schema/deployment change or current coverage claim was made.
