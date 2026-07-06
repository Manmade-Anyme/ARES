# Ares Build Log

A chronological log of session updates, technical decisions, and validation steps for the ARES Nifty 50 options trading system.

---

## 2026-07-06 15:45 · Separate Discord Channel for Observation-Only Alerts (TASK-178)

User's main channel is getting spammed by observation-only alerts (exhaustion MEDIUM signals, the trend-filter-downgraded HIGH signals) — several of these can fire in a single session while tradeable signals are rare by design. Small, contained fix: route `alert_only` signals to a second, optional Discord webhook instead of the main one. TDD: `tests/unit/test_task178_observation_discord_channel.py` (5 tests) written before the implementation. 255 tests green (250 → 255).

**Decisions**
- **New optional secret** `discord_observation_webhook_url` (`config.py`), same convention as the existing `discord_health_webhook_url` — `None` default, `.env.example` documents it.
- **Routing in `send_discord`** (`alerts.py`): if the signal is `alert_only` *and* the observation webhook is configured, post there; otherwise fall back to the main webhook. Tradeable signals always use the main webhook, unconditionally.
- **Fallback is the safety net**: nothing changes for the current deployment until the user creates the Discord channel + webhook and sets the secret — no risk of silently dropping alerts if the new webhook is ever misconfigured or unreachable (same try/except-log pattern as the other alert senders).
- `send_trade_update` untouched — it only ever fires for tracked (tradeable) trades, since `alert_only` signals are never picked up by `PositionManager`.

**User action required (not something I can do for them)**: create a new text channel in the Discord server, add a webhook to it (Channel Settings → Integrations → Webhooks → New Webhook → Copy URL), then set `DISCORD_OBSERVATION_WEBHOOK_URL` in both the local `.env` and `fly secrets set DISCORD_OBSERVATION_WEBHOOK_URL=...` (the latter triggers a new release — hold until the current deploy backlog from TASK-177 is resolved, per the standing "hold off, try later" decision on Fly's stalled depot builder).

**Status**: Branch `feature/TASK-178-observation-discord-channel`, not yet merged — awaiting PR review. 255 tests green.

**TODOs**
- [ ] Open PR, user review, merge.
- [ ] User creates the Discord channel + webhook and sets the secret (both `.env` and Fly).
- [ ] Deploy (bundled with the still-pending TASK-177 deploy) once Fly's builder is healthy again.

---

## 2026-07-06 15:10 · Trend Continuation Detector — 4th Setup, Observation-Only (TASK-177)

Follow-up to the Dhan-verified obs-signal analysis: 03→06-Jul was a 3-session grind-up with zero tradeable output, because all three existing detectors (Failed Breakout, OI Wall, Exhaustion) fade the move and the trend filter correctly blocks the counter-trend candidates they produce in a trending market. Root cause is a missing capability, not mis-tuned gates (every gate-loosening candidate was checked against Dhan data and only re-admits historically losing flow). ADR + directive written first (`directives/adr/TASK-177_trend-continuation-detector.md`, `directives/TASK-177_trend-continuation-detector.md`), then TDD: `tests/unit/test_continuation.py` (12 tests, pure state-machine unit tests) and `tests/unit/test_task177_trend_continuation.py` (7 tests, engine wiring) written before `detectors/continuation.py`. 250 tests green (231 → 250).

**Decisions**
- **New detector** `detectors/continuation.py` (`TrendContinuationDetector`, `SetupType.TREND_CONTINUATION`): state machine regime-persistence → armed → pullback → resumption. Regime uses the exact VWAP+PDH/PDL rule as engine Filter E, so signals are trend-aligned by construction and pass Filter E untouched — proven in `test_trend_filter_never_downgrades_aligned_continuation_signal`.
- **Design bug caught by TDD, fixed before merge**: the first draft used the strict Filter E formula to decide when a pullback "broke the regime." Since PDH/PDL normally bracket VWAP, that formula reduces to "any dip below VWAP is a regime break" — it would have aborted almost every real pullback immediately. Fixed by splitting the break condition: pre-arming uses the strict rule (an unproven candidate that flips isn't a real regime), but once armed, only a genuine PDL/PDH breach (a structural failure) aborts — a VWAP-side dip is the pullback itself. `test_hard_break_during_pullback_aborts` / `test_vwap_dip_during_pullback_does_not_abort` lock in the distinction.
- **Scoring**: 4-condition matrix (shallow pullback held the trend side of VWAP, resumption volume ≥1.2x/1.3x average, regime persisted ≥2x the arming minimum, room to the next opposing structural level), shared `confidence_from_score` 60% HIGH bar, `continuation_min_score` gate (2 non-expiry / 3 expiry) — below it, no signal at all.
- **SL/targets**: SL exactly at the pullback extreme, no buffer (TASK-175 convention); targets via the same shared structural target selection as breakout/exhaustion, with the standard T1-closer-than-T2 ordering fix.
- **Engine wiring**: priority slot 3 of 4 (breakout → OI wall → continuation → exhaustion), gated by `continuation_enabled` (master switch) and a Filter D2 `continuation_alert_only` observation gate mirroring exhaustion's Filter D — phase 1 ships alert-only, same "prove it before it trades" path exhaustion is still on.
- **Expiry disabled outright** (`continuation_enabled=False` on `EXPIRY_CONFIG`) until proven on non-expiry data — expiry moves die too fast for untested pullback logic.
- **Pre-merge Dhan replay** (`scratchpad/continuation_replay.py`, not committed): ran the real detector module against the 6 cached Dhan sessions (29-Jun→06-Jul) from the earlier obs-signal analysis — the only history available; a true 10-session replay would need more Dhan history than was fetched. Result: **15 signals, all bullish (matching the grind-up), all clearing the R:R gate (2.0–11.3), net +154.6 pts** under the user's trading model (40% at T1, SL→cost, runner to T2) — including one signal each on 03-Jul and 06-Jul, the exact two days that produced zero tradeable output live. This is a raw-detector number (bypasses the engine's speed/IV-crush filters, which would only remove weaker candidates), so treat it as an upper bound, but it's a strong signal the gap is real and fillable.

**Status**: Merged to `main` via [PR #23](https://github.com/dubeyshantanu2/ARES/pull/23). Local branch `feature/TASK-177-trend-continuation` deleted after merge. 250 tests green.

**TODOs**
- [x] Open PR, user review, merge.
- [ ] Run detector_scores collector fix (separate open task) before relying on live near-miss data for tuning `continuation_*` defaults.
- [ ] Accumulate ≥10 live/replayed observation signals, then a separate config-flip PR to set `continuation_alert_only=False` (phase 2).
- [ ] Tune and enable expiry-day continuation (`continuation_enabled=True` on `EXPIRY_CONFIG` with expiry-appropriate `continuation_*` values) — deferred until the non-expiry observation phase produces enough data to derive faster-candle-appropriate settings; expiry stays off blind until then.
- [ ] HIGH-exhaustion re-enable question (carried from the 2026-07-06 obs-signal analysis) remains open and separate from this task.

---

## 2026-07-06 10:41 · Observation Alert SL/Targets Restored (TASK-176)

Immediate user follow-up to the TASK-175 observation-alert restyle: the fully stripped card went too far — user wants the spot-level SL and targets back for evaluating observations against structure, while keeping the loud header and still omitting anything option-tradeable. TDD flow: updated the TASK-175 observation test to the new spec first (failing), then implementation. 231 tests green (count unchanged — one test rewritten).

**Decisions**
- **SL + T1/T2 restored** to `alert_only` alerts in both `send_discord` (embed fields) and `format_signal` (text fallback), same "(Spot Ref)" / "T1=… | T2=…" formatting as tradeable alerts.
- **Entry zone stays out** (user asked only for SL/targets; an entry range on a "NOT A TRADE" card invites exactly the #2056 mistake), and the **option sizing card stays out** (lots, option entry/SL/target) per explicit user direction.
- Header, gray color, and PositionManager non-tracking behavior unchanged from TASK-175.

**Status**: Merged to `main` via [PR #22](https://github.com/dubeyshantanu2/ARES/pull/22). Local branch `feature/TASK-176-obs-alert-sl-targets` deleted after merge. 231 tests green.

**TODOs**
- [x] Merge PR #22 and perform cleanup.

---

## 2026-07-06 10:20 · SL Buffer Removal, Observation Alert Restyle, Config Audit (TASK-175)

Follow-up to signal #2056: the user asked why the SL "hit" produced no Discord close — answer: the trend filter had made #2056 observation-only (`alert_only`), and such signals are never tracked by the PositionManager, but the alert still showed a full trade card, so it was traded manually. TDD flow: 10 failing tests first (`tests/unit/test_task175_sl_config_obs.py`), then implementation. 231 tests green (221 → 231).

**Decisions**
- **SL buffers removed (user direction, all 3 detectors)**: stop sits exactly at the structural reference — breakout level (was level ± 25/15), OI wall strike (was ± 25/15), exhaustion candle extreme (was ± 20). Config fields deleted rather than zeroed. Consequence: #2056-style trades stop 25 pts sooner; tighter risk also lets more setups pass the `min_rr_ratio` gate.
- **Observation-only alert restyle**: `alert_only` signals render with a gray "👁️ OBSERVATION ONLY — NOT A TRADE" title and no entry/SL/target/sizing fields (both the embed and the text formatter). Tradeable alerts unchanged.
- **Hardcoded → config sweep**: `breakout_deep_close_pts` (5.0), shared target-selection distances (`structural_target_min_distance_pts` 20 / `target_1_fallback_min_pts` 15 / `target_2_fallback_min_pts` 30 — the same literals were duplicated in all 3 detectors), `oi_wall_conviction_multiplier` (1.5), `oi_wall_wick_min_range_pts` (2.0), `exhaustion_extreme_volume_factor` (1.5), `exhaustion_extreme_doji_factor` (0.5), `exhaustion_level_proximity_pts` (10.0), `exhaustion_volume_history_size` (20), `trade_dedupe_tolerance_pts` (1.0), `iv_crush_min_samples` (10). All same-value in both profiles — pure refactor, zero behavior change.
- **Two latent bugs found by the sweep**: `breakout_resistance_proximity` was defined in config but never read (deleted); the OI wall wick-scoring block hardcoded 0.4 instead of using the existing `oi_wall_wick_rejection_ratio` setting (now honored — same default, so no behavior change until tuned).
- Exhaustion's volume-history size is read at construction; `main.py` applies the profile before building the engine, so this is safe (documented in the detector docstring).

**Status**: Merged to `main` via [PR #21](https://github.com/dubeyshantanu2/ARES/pull/21). Local branch `feature/TASK-175-sl-buffer-obs-alerts-config` deleted after merge. 231 tests green.

**TODOs**
- [x] Merge PR #21 and perform cleanup.
- [ ] Watch stop-out rate with buffer-less SLs — wick-outs at the exact level may argue for a small buffer via config re-introduction.
- [ ] Decide on trend-filter escalation for counter-trend HIGH signals (carried from TASK-174).

---

## 2026-07-06 09:53 · Breakout OI Scoring Fix (TASK-174)

Triggered by live signal #2056 (06-Jul 09:23, FAILED_BREAKOUT BEARISH at 24350, HIGH confidence, stopped out at 24375): 2 of its 4 confidence points came from a single OI reading (`writers_holding` at zero-change tolerance + `writers_active` at the hardcoded 3% bar), and the trend-regime filter had flagged it counter-trend (observation note only for HIGH). TDD flow: 8 failing tests first (`tests/unit/test_task174_oi_scoring.py`), then implementation. 221 tests green (213 → 221, one stale TASK-172 test updated to the new matrix).

**Decisions**
- **`writers_holding` unscored**: `atm_oi >= atm_oi_prev` is satisfied by zero change (noise), and any growth past the active threshold implies holding — scoring both double-counted one OI reading. It stays as a Discord reason string ("did not cover") for context. Deliberately *not* OR-merged with `writers_active`: since active ⊂ holding, an OR-merge would have made the growth threshold dead code.
- **`writers_active` threshold → config**: hardcoded 3.0% becomes `breakout_writers_active_min_pct` = 10.0 non-expiry / 15.0 expiry (expiry matches the stricter `oi_wall_min_oi_change_pct` stance). History: the original system used 20% for OI-wall growth; 3% was introduced with TASK-013's 6-point matrix and never tuned. Intraminute ATM OI drift of 3-5% is common noise per user's live observation. Boundary is inclusive (`>=`). Discord reason string now renders the configured value instead of a stale "3%".
- **Matrix 5 → 4 conditions** (weak volume, IV falling, writers active, deep close); `breakout_failure_min_score` stays 3, so a signal needs 3 of 4 genuine confirmations (was 3 of 5 where 2 could come from one OI reading). `confidence_from_score(max_score=4)` — every emitted breakout remains HIGH by construction (3/4 = 75% ≥ 60% band), same as post-TASK-172.
- **Replay of signal #2056 under new rules**: weak volume (1) + deep close (1) + writers active only if OI growth ≥ 10% → likely score 2 → no signal at all.
- Trend-filter escalation (blocking counter-trend HIGH instead of observation-only) explicitly deferred — separate task, needs more live data.

**Status**: Merged to `main` via [PR #20](https://github.com/dubeyshantanu2/ARES/pull/20). Local branch `feature/TASK-174-breakout-oi-scoring` deleted after merge. 221 tests green.

**TODOs**
- [x] Merge PR #20 and perform cleanup.
- [ ] Watch live breakout signal frequency — 10% may need loosening if signals dry up entirely.
- [ ] Decide on trend-filter escalation for counter-trend HIGH signals (deferred from this task).

---

## 2026-07-03 02:11 · Audit P2 Structural: Trend-Regime Filter & WS Tick Exits (TASK-173)

Implemented 2 of the 5 P2 "structural" items from the 2026-07-02 trade-efficiency audit (items 16 and 18; 14–15 stay `[SKIP]`, 17's full threshold sweep remains separate open scope). TDD flow: failing tests first (TypeError on the new `tick()` params, ModuleNotFoundError on the new `fetchers/tick_feed.py`, AttributeError on the new main.py helper), then implementation. 213 tests green (32 new since TASK-172's 181).

**Decisions**
- **Trend-regime filter (item 16)**: new engine Filter E derives regime from VWAP + PDH/PDL position — uptrend when close is above both VWAP and PDL, downtrend when close is below both VWAP and PDH, otherwise ambiguous (no action). Counter-trend HIGH confidence signals are downgraded to observation-only (`alert_only=True`, reusing the exact convention exhaustion's Filter D already established) rather than discarded; counter-trend MEDIUM signals are suppressed outright, matching the speed/IV-crush filters' existing MEDIUM-suppression convention. New `trend_filter_enabled` config flag (default `True`, both profiles). `AresEngine.tick()` gained optional `pdh`/`pdl` params (default `None`) so the filter no-ops — rather than raising — for any call site that omits them; all 3 pre-existing test files calling `tick()` with the old 5-arg signature needed no changes.
- **WebSocket tick feed (item 18)**: new `fetchers/tick_feed.py` wraps dhanhq's bundled `MarketFeed` WS client (confirmed present in the installed SDK — no new dependency) in Ticker mode (lowest bandwidth, LTP only) subscribed to the existing `settings.security_id`/`IDX` instrument. Runs on dhanhq's own background thread; latest LTP cached behind a lock. Purely additive: `main.py`'s new `_sleep_with_tick_exits` helper replaces the flat 60s `asyncio.sleep` — when the feed is active it wakes every `tick_exit_check_interval_seconds` (default 2.0s) and calls the *existing* `PositionManager.update_trades(price)` (a tick is just a degenerate single-price candle, so no new exit-check method was needed — pure reuse of the TASK-172 intrabar logic). SL_HIT still clears the engine cooldown, same as the 60s path. If the feed fails to start or errors out, `main.py` logs a warning and the loop behaves exactly as it did pre-TASK-173 (REST-only). The feed is stopped at session end and restarted alongside the REST clients on the existing 401/auth credential-reload path, since it authenticates with the same token.
- Deliberately did not touch `[SKIP]` items 14 (premium tracking) and 15 (per-detector cooldowns), and left item 17 (full ml_collection threshold sweep) as separate open scope per user direction this session.

**Status**: Merged to `main` via [PR #19](https://github.com/dubeyshantanu2/ARES/pull/19). Local branch `feature/TASK-173-p2-trend-regime-ws-exits` deleted after merge. 213 tests green (32 new). Coverage on touched modules: config_profiles 100%, `fetchers/tick_feed.py` 100%, engine 99% (one pre-existing unreachable branch, unrelated to this change). `main.py`'s new `_sleep_with_tick_exits` helper is fully unit-tested; the rest of `main.py`'s orchestrator loop remains integration-only, as it was before this task.

**TODOs**
- [x] Merge PR #19 and perform cleanup.
- [ ] Validate trend-regime filter and WS tick exits against live Discord output over the next sessions.
- [ ] Full threshold sweep against ml_collection dataset (audit item 17, remaining scope).

---

## 2026-07-03 01:25 · Audit P1 Tuning & Data-Quality Fixes (TASK-172)

Implemented the P1 block from the 2026-07-02 trade-efficiency audit (items 8, 10–13; item 9 time-of-day gates remains `[SKIP]`). TDD flow: failing tests first (import error + assertion failures confirmed), then implementation. 181 tests green (40 new/updated since TASK-171's 151).

**Decisions**
- **Breakout gate (item 8)**: `closed_back` removed from the failure score — it is the mandatory trigger and counting it gave every failure a free point. `breakout_failure_min_score` default 2→3 on the new 5-condition matrix. Consequence: every emitted breakout is now HIGH by construction (3/5 = 60% bar); the old "MEDIUM" breakouts are rejected outright rather than merely suppressed in flat markets, which is stricter.
- **Anti-IV-crush v2 (item 10)**: HIGH confidence exempt; symmetric via a new `pe_iv_lookback` (bullish→CE IV, bearish→PE IV); lookback 20→60 samples (~1 hour of polls), percentile + size configurable (`iv_crush_percentile`, `iv_crush_lookback_size`). Observation-only (alert_only) signals bypass the filter — they're never traded and suppressing them would lose exhaustion observation data.
- **Intrabar exits (item 11)**: `update_trades(spot, candle_high, candle_low)` — SL/T1/T2 touch checks use candle extremes (falls back to spot when omitted). Two accounting rules: *fill-at-level* (exits logged and alerted at the touched stop/target price, not the detecting close — symmetric honesty: stops no longer bleed detection slippage, T2 exits no longer overstate profit) and *pessimistic same-candle resolution* (candle spanning both stop and target = stop-out). SL is checked first; with close-only fallback the checks are mutually exclusive, so legacy behavior is preserved.
- **Confidence standardization (item 12/19)**: shared `models.confidence_from_score(score, max_score)` — HIGH at ≥60% of matrix. OI wall & exhaustion HIGH bar moves 2/4→3/4. Speed filter window/threshold now `speed_filter_window_candles`/`speed_filter_min_range_pts` in config profiles.
- **signal_id join fix (item 13/17)**: `log_signal` captures the inserted `ares_signals.id` into `signal.db_id` (new `AresSignal` field); `log_entry` writes it to `trade_analytics.signal_id` (bigint FK-style column already in schema — no migration). NULL only if the signal insert itself failed.
- **Timestamp tz fix (item 13/18)**: new `storage.to_utc_iso()` — naive timestamps are labeled IST (+05:30) then converted to UTC before write; aware timestamps convert without re-labeling. Applied to `ares_signals.timestamp` and `trade_analytics.entry_timestamp`; exits were already real UTC.
- **add_trade dedupe (item 13 / finding 4)**: skip a new trade if a non-closed active trade has the same setup_type + direction and entry within 1.0 pt (covers both double-call and restart-retrigger scenarios; re-entry after a close is unaffected).

**Status**: Merged to `main` via [PR #18](https://github.com/dubeyshantanu2/ARES/pull/18). Local branch `feature/TASK-172-audit-p1-tuning` deleted after merge. 181 tests green; coverage on touched modules: engine 99%, position_manager 99%, storage/models/config_profiles/breakout/exhaustion 100%, oi_wall 97% (pre-existing gaps).

**TODOs**
- [x] Merge PR #18 and perform cleanup.
- [ ] Validate new gates against live Discord output over the next sessions (P1 header's original caveat).
- [ ] Full threshold sweep against ml_collection dataset (audit item 17, remaining scope).
- [ ] Untagged P0 items pending decision: distinct BREAKEVEN exit type, candle timestamp dedup.

---

## 2026-07-02 19:40 · Audit P0 Efficiency Gates (TASK-171)

Implemented the four `[TODO]`-tagged P0 items from the 2026-07-02 trade-efficiency audit. TDD flow: 10 failing tests written first, then minimal implementation.

**Decisions**
- **R:R gate** (engine Filter C): reject signal when `reward/risk < min_rr_ratio` (new config, 1.0 both profiles). Risk = |trigger−SL|, reward = |trigger−T1|. Degenerate SL placement (risk ≤ 0) always rejected.
- **Exhaustion observation mode** (engine Filter D): `exhaustion_alert_only=True` tags exhaustion signals `alert_only` (new `AresSignal` field). `main.py` alerts + logs them but skips `add_trade`; alert-only signals do NOT consume the engine cooldown so they can never block a tradeable setup.
- **Time-stop** (`PositionManager._apply_time_stop`): OPEN trade older than `time_stop_minutes` (45 non-expiry / 30 expiry) without T1 → SL tightened to entry. Trade stays alive (multi-day carry intact); exits caused by the tightened stop are labeled `TIME_STOP`, never `T1_HIT`. In-memory `_time_stopped` flag only — no schema change; flag cleared if T1 is genuinely hit.
- **Cooldown reset after stop-out**: `update_trades` now returns `(trade_id, update_type)` events; `main.py` calls `engine.clear_cooldown()` on any `SL_HIT` so re-entry is unlocked immediately.
- **Backtest validation** (audit item 17, first pass): replayed last 10 days of trades against ml_collection minute data under the trader's 40/60 execution model — net -162.8pts → -36.7pts with gates active (10 exhaustion gated, 3 R:R-gated, time-stop zeroes drift losses). Full parameter sweep vs ml_collection remains open.
- `[SKIP]`-tagged audit items (time-of-day gates, option premium tracking, per-detector cooldowns) intentionally not implemented.

**Status**: Merged to `main` via [PR #17](https://github.com/dubeyshantanu2/ARES/pull/17). Local branch `feature/TASK-171-audit-p0-efficiency-fixes` deleted after merge. 151 tests green (10 new). Coverage on touched modules: engine 99%, position_manager 98%, config_profiles/models 100%.

**TODOs**
- [x] Merge PR #17 and perform cleanup.
- [ ] Full threshold sweep against ml_collection dataset (audit item 17, remaining scope).
- [ ] Monitor TIME_STOP/observation-only behavior in the next live sessions; tune `time_stop_minutes` with fresh data.

---

## 2026-07-02 18:30 · Multi-Day Trade Carry Fix + Trade Efficiency Audit (TASK-170)

Ran a full trade-efficiency audit (repo + Supabase live data, Jun 24 – Jul 2). Discovered `PositionManager._initialize_db` wiped all previous-date `active_trades` rows at startup, contradicting the intended multi-day tracking (ride trades until T1/T2/SL for later analysis). Live impact: 12 of 29 `trade_analytics` rows permanently stuck in `OPEN` with no exit logged.

**Decisions**
- Removed the previous-date deletion block from `_initialize_db`; all rows with state not in (`CLOSED`, `STOPPED_OUT`) now load into memory regardless of date. Closed rows remain in the table (`trade_analytics` stays the permanent record).
- Overnight gaps are handled implicitly: the first poll next session closes gapped trades at the first tick.
- EOD force-close (an earlier audit recommendation) explicitly withdrawn — multi-day carry is intended behavior.
- Added `scratch/restore_orphan_trades.py` (local-only, dry-run default, `--apply` to write): rebuilds the 12 orphaned trades into `active_trades` by recovering T1/T2/SL from `ares_signals` via setup+direction+spot(±0.6)+timestamp(±10min) matching (29/29 match rate validated).
- Replaced the old-date-wipe unit test with multi-day carry tests: cross-date loading, T1_HIT resume with trailed SL, closed-row exclusion, and a carryover trade gapping past T2 → CLOSED.
- Full audit findings published to `docs/ARES Trade Efficiency Audit 2026-07-02.md` and mirrored to the Obsidian vault (`Projects/Ares/`). Headline: T1 reached in only 14% of trades; exhaustion detector is the main bleeder; no R:R gate; avg +6.8pts stop slippage from close-only 60s exit checks.

**Status**: Merged to `main` via [PR #16](https://github.com/dubeyshantanu2/ARES/pull/16). Local branch `feature/TASK-170-multi-day-trade-carry` deleted after merge. 141 tests green on `main`.

**TODOs**
- [x] Merge PR #16 and perform cleanup.
- [ ] Run `scratch/restore_orphan_trades.py --apply` after human confirmation to resurrect the 12 orphaned trades.
- [ ] Work the audit P0 backlog: R:R gate, time-stop/momentum target, exhaustion gating, BREAKEVEN exit type, candle dedup, cooldown reset after SL.

---

## 2026-07-02 14:10 · OI Wall Detector Confirmation-Candle Requirement (TASK-169)

Audited two weak/false OI Wall Rejection alerts (#2599, #0308) and found the detector's core flaw: it fired on a single candle's shallow touch of the wall, with no follow-through requirement. Rewrote `OIWallDetector` to require a confirming second candle before emitting a signal, mirroring the stateful pattern already used in `ExhaustionDetector`.

**Decisions**
- Converted `OIWallDetector.detect()` (stateless) to `OIWallDetector.update()` (stateful), tracked via `self.pending_setup`.
- A wall touch only becomes a candidate if it shows a genuine wick rejection ($\ge 40\%$ of candle range) — closes red/green alone no longer qualifies.
- The signal only fires if the *next* candle confirms by closing beyond the candidate candle's high/low; unconfirmed candidates expire after exactly one follow-up candle (no indefinite pending state).
- Updated `engine.py` call site from `.detect(...)` to `.update(...)`.
- Rewrote `tests/unit/test_oi_wall.py` detect-flow tests into two-call confirmation flows; added coverage for confirmed/unconfirmed bearish and bullish setups, candidate expiry, and rejection of candles lacking a genuine wick.
- Stop-buffer and confidence-bar tuning (also flagged in the audit) explicitly deferred to a future session.
- **Review follow-up**: moved the wick rejection ratio out of a hardcoded constant into `config_profiles.py` as `oi_wall_wick_rejection_ratio` (0.4 in both profiles), per reviewer request to keep it tunable. Also removed the committed `scratch/oi_wall_audit_20260702.md` audit file from the PR (scratch/ is local-only), added `scratch/` to `.gitignore`, and logged a `TODO.md` item to clean up any pre-existing tracked scratch files.

**Status**: Merged to `main` via [PR #15](https://github.com/dubeyshantanu2/ARES/pull/15). Local branch `feature/TASK-169-oi-wall-confirmation-candle` deleted after merge.

**TODOs**
- [x] Merge PR for `feature/TASK-169-oi-wall-confirmation-candle` and perform cleanup.
- [ ] Revisit `oi_wall_stop_buffer` (currently 25pts NON_EXPIRY) and the confidence HIGH threshold (currently score >= 2) per the audit's remaining findings.
- [ ] Audit `scratch/` for files still tracked in git from before the `.gitignore` change and remove them.

---

## 2026-07-02 00:35 · Option IV Ingestion Bug and Sluggish Market Filters

Fixed a silent IV and Greek parsing bug in the option chain fetcher and ML live predictor. Integrated a 15-minute rolling range speed filter and an Anti-IV Crush filter in the core execution engine to prevent entries in sluggish market conditions where theta decay dominates.

**Decisions**
- Corrected option chain parsing in `OIFetcher` and `LiveRunner` to use Dhan API's native `"implied_volatility"` key.
- Nested Greek metrics extraction inside the `"greeks"` sub-dictionary for the live XGBoost prediction runner.
- Added a 15-minute Nifty Spot rolling range filter in `AresEngine` to suppress `MEDIUM` confidence setups when range is $< 15.0$ points.
- Added an IV percentile lookback filter in `AresEngine` to suppress Call entries when current IV falls in the top 90% of its 20-candle lookback.
- Added new unit test files `test_oi_fetcher.py`, `test_ml_live.py`, and `test_engine_remediation.py` to maintain 100% test coverage.

**TODOs**
- [x] Merge PR #14 and verify.
- [ ] Implement the styling enhancement to visually flag "Paper Sizing Only" signals on Discord when suggested lots is 0.

---

## 2026-07-02 00:33 · Workspace Rules Git and PR Flow Consolidation

Merged the separate **Commit & Push** and **Raise PR** steps in `.agents/AGENTS.md` into a single, unified step 3 to streamline the pipeline workflow for future agent sessions.

**Decisions**
- Combined the branch pushing and PR creation instructions into a single cohesive Step 3 under Section 2 of `AGENTS.md`.
- Renumbered the **Merge Cleanup** step to Step 4.
- Updated `CHANGELOG.md` to reflect this change.

**TODOs**
- [ ] Raise PR and verify.

---

## 2026-07-01 00:02 · Discord Alert Bold Markdown Formatting Fix

Fixed a bug in `alerts.py` where bold (`**`) styling inside Discord alerts was displayed literally or incorrectly because the entire alert details were wrapped inside a ````diff` code block. Closed the code block after the header line to allow Discord to correctly parse and render bold markdown.

**Decisions**
- Closed ````diff` blocks immediately after the color-coded header line in `format_signal` and `send_trade_update`.
- Kept raw unicode emojis for consistency and layout structure.
- Re-run and verified the test suite.

**TODOs**
- [x] Merge PR #13 and verify.

---

## 2026-06-30 23:45 · Updated README with Option Sizing, Confidence Scoring, and Testing details

Updated the main `README.md` documentation to reflect recent changes to ARES, including option sizing & delta-based strike selection, upgraded 6-point/4-point dynamic confidence scoring matrices for detectors, dynamic targets for the OI Wall Rejection detector, and instructions for running the newly achieved 100% test coverage unit test suite.

**Decisions**
- Documented `options_math.py` integration, explaining delta strike selection (target 0.45, range 0.45-0.55), capital-aware ingress, and risk-managed lot sizing formulas.
- Updated the "Detection Strategies" section in `README.md` to detail the upgraded 6-point scoring for Failed Breakout and the 4-point dynamic scoring for OI Wall Rejection and Exhaustion Reversal.
- Added pytest execution guidelines under a new "Running Unit Tests" subsection in the Setup and Deployment section.
- Added `options_math.py` to the tree under the "Project Structure" directory layout.

**TODOs**
- [x] Merge PR #12 and verify.

---

## 2026-06-30 19:55 · Redeployed ARES to Mumbai (bom) & Updated PR Workflow Rules

Redeployed the ARES application (`ares-xzy-gq`) back to the Fly.io Mumbai (`bom`) region from Singapore (`sin`) to resolve Discord signal/response latency issues. In addition, updated the repository's `.agents/AGENTS.md` guidelines to require detailed PR descriptions outlining the Problem, Solution, and Testing/Verification performed.

**Decisions**
- Redeployed ARES to the `bom` region via `fly deploy` using the existing `fly.toml` configuration.
- Verified the machine version `61` in the `bom` region.
- Appended the PR description workflow requirements under step 4 of Section 2 in `.agents/AGENTS.md`.
- Merged the rule change to `main` and cleaned up the local feature branch `feature/TASK-165-redeploy-bom`.

**TODOs**
- [x] Merge PR #11 and verify deployment.
- [x] Merge the rules updates to `main` and perform local branch cleanup.

---

## 2026-06-29 21:40 · Discord Alert Formatting Upgrades

Refactored the Discord signal alert and trade update alert formatting in `alerts.py` to use textual emoji shortcodes (e.g. `:clock3:`, `:round_pushpin:`, `:white_check_mark:`, `:octagonal_sign:`, `:dart:`, `:zap:`, `:star:`, `:triangular_ruler:`, `:1234:`, and `:pencil:`) and bold tags around values for high visual contrast and modern appearance. Removed the horizontal divider trailing lines and indented Option Sizing Calculator fields.

**Decisions**
- Relayout signal template formatting with custom emoji shortcodes.
- Apply bold tags to all entry, stop-loss, target, and status values.
- Keep the ````diff```` wrapper to enable syntax coloring where needed, but align emojis and bolds to render gracefully.
- Remove horizontal divider lines `━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━` at the end of alerts to reduce alert vertical size.

**TODOs**
- [ ] Merge PR for feature branch `feature/TASK-164-discord-alert-formatting` and perform cleanup.

---

## 2026-06-29 18:02 · Dynamic Structural Targets for OI Wall Rejection

Implemented dynamic structural target selection for the `OI_WALL_REJECTION` setup in `OIWallDetector` (aligning it with breakout and exhaustion detectors). The system now identifies the next significant support/resistance levels from the option chain and structural data to set realistic exit points instead of fixed offsets.

**Decisions**
- Upgraded the `OIWallDetector.detect` and `_build_signal` methods to accept the structural `levels` list.
- Configured dynamic target selection with a minimum 20-point target proximity filter and proximity-based sorting.
- Maintained a fallback to configured fixed target offsets (`settings.target_1_pts` / `settings.target_2_pts`) if structural levels are unavailable or too tight.
- Updated `engine.py` to correctly forward the `levels` list during the engine tick evaluation.

**TODOs**
- [ ] Merge PR for feature branch `feature/TASK-163-oi-wall-dynamic-targets` and perform cleanup.

---

## 2026-06-29 14:25 · 100% Test Coverage & Workspace Rules

Upgraded the project unit test suite to achieve 100% line coverage on all target logic and adapter files. Designed a pre-import reloading patch for the Supabase create_client connection to isolate DB persistence. Added project-scoped rules for automatically executing the Global Development Pipeline in the ARES workspace.

**Decisions**
- Isolate external integrations (Dhan API and Supabase) by reloading imports within mock decorators.
- Enforce the Global Development Pipeline rules by creating a project-scoped AGENTS.md file in the workspace root.
- Validate all retry patterns and exception paths (such as event loop runtime failures) within the testing suite.

**TODOs**
- [x] Merge PR #8 and perform local branch merge verification and cleanup.
