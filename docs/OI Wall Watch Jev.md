# Jev assessment in OI-wall ready watches

TASK-211 adds one “🧠 JEV Prediction” field to the existing OI_WALL_RETEST_READY embed. Existing title, timestamp, spot, wall, persistence, guidance and footer are preserved. The field contains an estimated closing-outcome probability, current price-structure outlook, grounded evidence and assessment timestamp. It is delivered in the first message, never a separate watch follow-up. Confirmed-signal T1/SL/T2 Jev questions and alerts keep their existing behavior.

## Forecast definition

For bearish watches the event is the final regular-session spot finishing strictly below `watch spot - minimum_move_points`. For bullish watches it is strictly above `watch spot + minimum_move_points`. The initial configurable minimum move is 25 spot points. Exact threshold and move are displayed and archived. This is a forecast of the session endpoint, not a guarantee of a continuous trend or an option-premium profit.

Noul supplies the probability of that event. Choice describes observed price structure as established, fragile, range-bound, opposing or insufficient evidence. Three independent Score judgments measure wall support, price alignment and room before opposing structure. Scores do not become a closing probability through averaging. Supporting text comes from captured numeric evidence, not generated model explanations.

The model sees only the frozen first-ready observation: wall metrics, actual filter interaction/excursion, refreshed candle history, recent movement, session-so-far range, structural levels, available option-chain OI/IV and expiry context. History comes from the same Dhan response already fetched by the application, without an additional request. A previously sampled partial candle is never called finalized merely because its minute later ended. Longer windows are missing during warmup. Future and other-session candles and future sample timestamps are excluded. Missing VWAP remains unavailable; no generic ML zero placeholder is treated as an observed VWAP. No account balances or private trading-history strings enter the state.

## Delivery and lifecycle

The application captures an immutable watch UUID and snapshot, then queues persistence using a background writer with its own client. The independent Jev subprocess handles watch inference and delivery on separate loops. The trading loop does not wait for these operations. Original payload and raw results are persisted in `oi_watch_predictions`, outside confirmed-signal tables.

Before posting a confirmed OI-wall signal, the application fences queued watch lifecycle changes for up to five seconds and waits up to six seconds for already-in-flight watch transport to settle. It never waits for Jev inference. If a fence fails, the confirmed signal still proceeds and ordering is logged as degraded. A subsequently confirmed late watch receives a closure using the existing cancellation format, with reason “Watch superseded by confirmed signal”; it is not left as a fresh actionable observation. Network delivery with an unconfirmed UNKNOWN outcome remains unconfirmed and is never blindly replayed.

The worker sends when the result arrives, inference fails, or the configured waiting deadline expires. Failure/timeout appends “Jev assessment unavailable” in the same field. One database claim owns each send; no later watch prediction is posted separately. A consumed, canceled or replaced watch is suppressed before send. A cancellation arriving during an accepted watch request is delivered afterward using the existing cancellation template. Successful final-signal delivery resolves that watch.

Ambiguous webhook outcomes are marked UNKNOWN rather than blindly replayed. Explicit rejected requests can retry subject to freshness. Restart recovery preserves accepted/ambiguous delivery state and terminates unresolved prior application-run watches. Exact inputs, model/question/context versions, probabilities, timestamps and transport outcomes remain available for audits.

## Rollout

1. Apply reviewed `migrations/2026-10-08-task211-oi-watch-jev.sql` to the intended project before enabling the new watch path. It creates service-role-only RLS storage and lifecycle RPCs; existing confirmed-signal tables are unchanged.
2. Provision `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `TYPESAFE_API_KEY` and `DISCORD_WEBHOOK_URL` on the backend. Keep keys outside source code and client applications.
3. Edit `OI_WATCH_JEV_CONFIG` in `config_profiles.py`. Both processes use this shared file configuration; `.env` contains credentials only:

   ```python
   OI_WATCH_JEV_CONFIG = OIWatchJevConfig(
       oi_watch_jev_enabled=True,
       oi_watch_jev_minimum_move_points=25.0,
       oi_watch_jev_wait_seconds=8.0,
       oi_watch_jev_max_age_seconds=60.0,
   )
   ```

4. Deploy through the existing main-branch workflow. Latest main starts the application and independent Jev subprocess through `scripts/run_app_with_jev.sh` on the scheduled app Machine. No new Fly process group, Machine or scheduler is required.
5. Verify outbox readiness and worker startup logs. A missing migration/service-role setup must not stop existing trading or confirmed-signal inference; the application retains its direct watch fallback.

The percentages are experimental model estimates until prospectively calibrated on distinct trading days. The retained seven-watch audit motivated this feature but cannot validate probability accuracy. Historical and future actual outcomes must remain outside inference inputs.

## Verification

Unit tests cover context boundaries, typed questions, result validation, exact original embed preservation, same-message fallback, filter lifecycle, writer ownership and worker failures. `tests/integration/task211_watch_lifecycle.mjs` executes the migration against an isolated PostgreSQL engine; it must never use the production database. Read the task QA report for completed checks and remaining rollout steps.
