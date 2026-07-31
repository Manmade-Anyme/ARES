# Ares Build Log

A chronological log of session updates, technical decisions, and validation steps for the ARES Nifty 50 options trading system.

---

## 2026-07-31 · ml_collection Had No Join Key, So It Had No Labels (TASK-194)

User asked what `ml_collection` had actually collected and whether it was doing a useful job. Audit of all 9,102 rows (2026-06-29 → 07-31, 25 trading days, median 374 rows/day — a complete per-minute tape) found the collector mechanically healthy and the table untrainable.

**What the audit found**

| Finding | Measured |
|---|---|
| `trade_outcome` / `trade_pnl` / `trade_id` populated | **0 of 9,102** |
| `ml_collection.signal_id` ∩ `ares_signals.id` | **0 of 119** |
| `detector_scores` carrying info beyond `signal_setup_type` | **none** — hot key matched in 100% of rows, 0 mismatches |
| `structure_features` at the `100.0` sentinel | **57%** of rows |
| `dte` correct | 12% (stuck at 7.0 until the 07-28 fix) |
| `pcr_oi` / `oi_concentration` usable | 35% (pinned to the 1.0 divide-guard until PR #46 on 07-21) |

**Root cause — a key, not a missing write.** `MLCollector.snapshot` stored `signal.signal_id`; `models.AresSignal` defines that as `f"{random.randint(0, 9999):04d}"`, a Discord display code. `AnalyticsLogger.add_trade` stores `signal.db_id`, the real `ares_signals.id`. The two tables have never been joinable, so there was no row to attribute an outcome to. `main.py` also ran the snapshot **before** `storage.log_signal`, the call that assigns `db_id` — so even the correct field would have read `None` every time.

**Fixed**: collector writes `db_id` (NULL when unset, never a fabricated key); snapshot moved after the signal block; `storage.log_exit` writes the three label columns on close and skips cleanly on a NULL `signal_id`. `log_exit` also gained the `except RuntimeError` fallback `MLCollector._insert` already had — without a running loop it previously printed an error and performed **no update at all**.

**Also fixed, same pass**: `compute_oi_features` was handed the full per-strike OI distribution and used it only to compute a sum, discarding the shape — which is precisely why the p85 wall-threshold proposal was untestable against history. Now stores `max_*_oi`, `p85_*_oi`, `strikes_with_*_oi` (nearest-rank, non-interpolated, zero-OI strikes excluded). And `compute_structure_features` no longer returns `100.0` for "unknown" — a sentinel that 57% of rows carried and that no amount of further recording would have healed.

**History repair**: `ml_signal/backfill_labels.py`, dry-run by default. Production dry run: **120/120 join keys repairable** (matched on `created_at` within 120s + normalised `setup_type`; observed clock deltas sub-second, median 0.1s), **90 labels writable**, 0 unmatchable. 36 trades cannot be labelled — their own `signal_id` is NULL. Legacy rows storing `str(SetupType.X)` are normalised during the match.

**Not run** — user-run like the TASK-188 migration. 305 tests green (12 new).

- [ ] Run `python -m ml_signal.backfill_labels --apply` against production.
- [ ] Re-audit label coverage after the next full trading day to confirm the live path writes.

---

## 2026-07-20 · Kronos Inference Needs 3.6GB on a 768mb VM — Removed Entirely (TASK-190)

User reported the ARES startup alert arriving in Discord repeatedly. The alert code was not at fault: the Fly machine was OOM-killed and restarted six times in three minutes, and `send_startup_alert()` fires once per boot.

**Timeline, 2026-07-20 (UTC)**

| Time | Event |
|---|---|
| 06:10:54 | Signal **#231** (TREND_CONTINUATION) written to `ares_signals` |
| 06:11:28 | Kronos consumer starts, sees #231 aged 34s (< 180s) → runs inference |
| 06:11:34 | `Out of memory: Killed process ... anon-rss:627072kB` |
| 06:11:44 | Machine reboots → Discord startup alert #1 |
| … | Cycle repeats at 06:12:38, 06:13:10, 06:13:46 |
| 06:14:15 | #231 aged past 180s → marked processed, no inference → loop ends |

The loop is **self-limiting but recurring**: it ends when the signal ages out of the 180-second recency window in `KronosConsumer.run`, and starts again on the next signal.

**Root cause.** `_autoregressive_paths` (`ml_signal/kronos_vendor/paths.py`) expands the context to `batch = sample_count` via `x.unsqueeze(1).repeat(...)`, then decodes autoregressively **without a KV cache** — re-running `model.decode_s1` over the whole window for each of the 45 prediction steps. Measured peak RSS (`ru_maxrss`, horizon 45, synthetic OHLC — memory depends on tensor shape, not price values):

```
context 1000 x 20 paths -> 3613 MB      <- shipped configuration
context  500 x 20 paths -> 2126 MB
context 1000 x  5 paths -> 1314 MB
context 1000 x  2 paths ->  820 MB
```

Inference costs **~160 MB per sampled path plus ~175 MB fixed**. Usable memory on the 768mb VM is ~710 MB and the live trading baseline is 340 MB (measured on the running machine via `/proc/<pid>/status`), leaving ~370 MB — which buys **exactly one path**, a probability that can only read 0% or 100%.

**The `anon-rss:627072kB` in the OOM log is misleading** and cost time during diagnosis: it is the RSS the kernel sampled at kill time, not the demand. It reads like a marginal 627-vs-768 overshoot; the process was climbing toward 3.6 GB.

**Superseded guesses.** The `ponytail:` comment at `kronos_consumer.py:68` prescribed dropping `KRONOS_CONTEXT_CANDLES` to 500 for exactly this signature. That was an unmeasured guess and is wrong by ~3× — 500 still peaks at 2.1 GB. The comment now carries the measured figures.

**Real cost of the loop.** The Discord spam is cosmetic; each restart zeroes VWAP and the candle buffers (`Buffers=1/30`), so ARES cannot score any setup for the following 30 minutes. Every signal cost ~33 minutes of blind time.

**Resolution: removed, not resized.** Kronos never posted a single probability in production across four tasks (TASK-184 built it, 186 inlined it, 187 fixed its context, 190 measured it). Rather than pay for a bigger machine to keep an unproven informational number alive, the whole subsystem is deleted.

**Deleted**
- `ml_signal/kronos_consumer.py`
- `ml_signal/kronos_vendor/` — the vendored transformer package and `paths.py`
- `ml_signal/data.py::load_intraday_candles_from_dhan` — Kronos was its only caller
- `tests/unit/`: `test_kronos_consumer.py`, `test_task186_in_process_kronos.py`, `test_task187_kronos_context_horizon.py`, `test_task190_kronos_not_in_process.py`, `test_ml_signal_data.py`

**Changed**
- `main.py`: `_start_in_process_kronos_consumer` and its `create_task` removed; banner line gone.
- `alerts.py`: the Discord **startup alert** hardcoded `[+] Kronos ML Engine : ACTIVE (NeoQuasar/Kronos-mini decoupled)` — this was in the very message that was spamming the channel, and it was unconditional, so it would have kept asserting ACTIVE. Removed.
- `ml_signal/config.py`: `kronos_horizon_candles` removed.
- **`torch`, `einops`, `huggingface_hub`, `safetensors` dropped** from both `requirements.txt` files, and the CPU-torch wheel install dropped from the `Dockerfile`. Nothing outside `kronos_vendor/` imported any of them — verified by grep before deleting. This is the bulk of the container image.

**Decisions**
- Kronos was informational only — it never touched signal generation, entries, or SL/T1/T2 — so the trading loop is unaffected by its removal.
- Rejected scaling the VM to 4 GB. It works and needs no code change, but pays continuously for a feature with no production track record.
- `load_intraday_candles_from_dhan` went with it as zero-caller code. Note the other four functions in `ml_signal/data.py` (`get_supabase_client`, `load_ares_trade_analytics`, `load_historical_candles_from_dhan`, `build_training_dataset`) **also have zero callers** — that file is now almost entirely dead and is a candidate for a follow-up prune, deferred here to keep this change scoped to Kronos.
- The offline XGBoost pipeline (`dataset.py`, `train_offline.py`, `trainer.py`, `predictor.py`, `models/v1.joblib`) is untouched — it never depended on Kronos.
- Three `Kronos` mentions remain in `schema.sql` and the TASK-188 migration. Those refer to a **different app** sharing the Supabase project (alongside Gamma Blaster, Phantom, Sniper, Order Flow) and must not be touched.

**Verification**
- 273 tests green (was 298; 25 removed with the deleted modules — no failures).
- `import main` succeeds with `torch` absent from `sys.modules`.

**TODOs**
- [x] Merged via [PR #42](https://github.com/dubeyshantanu2/ARES/pull/42) (`27ce5c3`, 2026-07-20). 273 tests green on `main`.
- [ ] **Deploy — not yet done.** Merge ≠ deploy; Fly still runs the pre-removal image. Deploy **after 15:30 IST** (a restart zeroes VWAP + buffers mid-session).
- [ ] Fix the Discord startup alert's `Session : 09:15 to 23:30 IST` line — `alerts.py` hardcodes 23:30 while `main.py` gates on `time(15, 30)`. Cosmetic, long-standing, unrelated to Kronos.
- [ ] Consider pruning the remaining zero-caller functions in `ml_signal/data.py` (`get_supabase_client`, `load_ares_trade_analytics`, `load_historical_candles_from_dhan`, `build_training_dataset`).

**Follow-up sweep** (`15820a2`). The first pass grepped only `*.py`/`*.txt`/`*.toml`/`Dockerfile`/`*.sql` and missed the markdown. Also removed: `directives/ADR-184_kronos-forward-forecasting-evaluation.md` and `directives/TASK-186_in-process-kronos-ml-bugfix.md` (both orphans — nothing in `directives/adr/INDEX.md` or any other doc referenced them), the `DEPLOYMENT.md` Dockerfile paragraph still describing the in-process consumer, and 10 stale Kronos permission entries in `.claude/settings.local.json`.

**Kronos history is deliberately retained** in this log and `CHANGELOG.md`. The code is gone; the record of the 3.6GB measurement and why it was removed is what prevents a future forecasting model rediscovering the same ceiling. The TASK-184/186 entries stay as history of a feature that existed and was withdrawn.

**Codex review note.** The bot flagged `alerts.py`'s hardcoded `Kronos ML Engine : ACTIVE` line — a correct catch against commit `92359ca`, which it reviewed. It never saw `12a2f48`, where the removal fixed the same line independently. Codex reviews only the commit that triggered it, so on multi-commit PRs expect findings already resolved by later commits.

---

## 2026-07-17 · OI Wall Never Fired — The Wick Gate Blocked Every Winner (TASK-188)

User reported OI wall rejection signals had stopped for days. Audit of the live system found **zero `OI_WALL_REJECTION` signals from 2026-07-02 through 07-17** — 11 trading days — while the other detectors fired 60× over the same span. Merged via [PR #41](https://github.com/dubeyshantanu2/ARES/pull/41).

**Root cause: TASK-169's wick gate, shipped 2026-07-02 — the exact date of the last signal.**

TASK-169 added two conjunctive gates to the candidate rule: a `>=40%` wick rejection and a next-candle confirmation. Replaying the closed OI-wall book against real Dhan 1-min candles showed they pull in opposite directions:

```
no gates                  : 8 trades, +30.05 pts, avg  +3.76
wick gate only            : 2 trades, -57.35 pts, avg -28.68   <- both losers
wick + confirm (prod)     : 1 trade,   -9.60 pts
confirmation only         : 4 trades, +79.35 pts, avg +19.84
```

All three real T2 winners had wicks of **19.0%, 24.4%, 21.6%** — every one under the 0.4 threshold. The gate blocks **3 of 3 winners** and admits only losers; the biggest loser (−47.75) had the *deepest* wick in the book at 57.9%. The audit that motivated TASK-169 (`scratch/oi_wall_audit_20260702.md`) states it could not fetch live data, and generalised "the trade likely failed" from the two losers that happened that same afternoon. On the real book, the shallow touches it wanted to filter out **were** the winners.

**Supersedes TASK-184's conclusion.** TASK-184 attributed OI-wall silence to the `oi_wall_min_oi_change_pct=5.0` per-60s gate. That gate is real (live probe 07-17: 29 walls passed the size gate, **0** passed the 5% gate) but its delta distribution is **unchanged since June** (median 0.63%/min, p99 ~10%, across 5,172 `ml_collection` rows). It makes signals *rare* (~2.4/day) — it is not why they stopped. The OI baseline was deliberately left untouched.

**Changes**
- **Dropped the wick gate** from `_find_candidate`. Wick depth still feeds the confidence score; it can no longer veto a setup.
- **Kept the next-candle confirmation** unchanged — the replay supports it.
- **Removed `writers_holding`** (dead code): wall selection already requires `oi_wall_min_oi_change_pct > 5%`, which implies `ce_oi > ce_oi_prev`; a zero `ce_oi_prev` forces `change_pct = 0.0` in `OIFetcher` so the wall fails selection anyway. It could never evaluate `False`. Its unchecked *"Option writers defended the level (OI did not drop)"* alert line was replaced with one the code verifies.
- **Fixed detector starvation** in `engine.tick()`: the cooldown `return`ed before any detector ran, and the `or` short-circuit skipped OI wall when breakout fired. Since TASK-169 made the detector **stateful**, a pending candidate could survive a 15-min cooldown and be confirmed against a much later candle — a missed signal *and* a wrong signal. It now advances every candle; only *emission* is gated.

**Test fixtures were leaking into production** — this corrupted the measurement itself. `tests/conftest.py` fetched live Dhan credentials at session start, importing `storage` unmocked before `test_storage.py` could patch `supabase.create_client`. When that import-order-dependent patch lost the race, `Storage()` bound a **real** client and `test_storage.py`'s `log_signal()` fixtures were written to production: `ares_signals` ids **167–170** (`spot=24001.0`, reason literally `"Reason 1"`, `Capital: ₹10,000.00`) plus **9** `trade_analytics` rows — including a fabricated **+99.0 T2_HIT** that inflated the OI-wall average from **+3.76 → +14.34**. All contaminated rows were `OI_WALL_REJECTION`, so only that setup's numbers were wrong. `conftest.py` now points Supabase and Dhan at unroutable dummies; no unit test needed live credentials.

**Validation**: TDD — `test_task188_oi_wall_wick_gate.py`, `test_task188_oi_wall_starvation.py`, `test_task188_test_db_isolation.py`. **297 tests green** (283 → 297).

**Honest caveats**
- **n=8.** The per-gate P&L splits are directional, not proven. The load-bearing fact is deterministic geometry: the wick gate blocks 3/3 winners.
- **OI wall is a marginal setup** at +3.76 avg — below `EXHAUSTION_REVERSAL` (+10.08) and `FAILED_BREAKOUT` (+28.15). This restores a modest edge, not a star.
- The replay **cannot perfectly reproduce** historical fires: historical option-chain OI isn't stored anywhere (only ATM reaches `ml_collection`), so the wall is synthesised and candle alignment is ambiguous. Patched fired 3/8 in replay vs 1/8 for prod — indicative, not measured.

**Open / follow-ups**
- `migrations/2026-07-17-task188-fixture-cleanup-and-ist.sql` — fixture purge + IST render setting. **User-run, still pending.** Deletes are constrained to the confirmed ids AND the full fixture signature (`reasons[0] = 'Reason 1'`, which production code cannot emit) after a Codex P1 flagged that a price-only match would destroy a real signal sitting at 24001.0.
- `breakout_detector` and `continuation_detector` are **also stateful and also starved** by the same cooldown/short-circuit. Left alone deliberately — FAILED_BREAKOUT is the best performer and there's no evidence its current rate is wrong.
- **Deploy pending.**

**Lesson**: never accept a detector-tuning audit that didn't measure outcomes. TASK-169's audit reasoned from plausibility ("a real rejection should have a deep wick") on a day it couldn't fetch data, and silently zeroed a working detector for 11 trading days while the test suite stayed green — because the fixtures hand-feed `ce_oi_change_pct: 11.1`, a value the live 60s window basically never produces.

---

## 2026-07-09 · Per-Setup-Type SL/T1/T2 Config — Single Source of Truth (TASK-185)

Turned the TASK-185 SL/target study into a shipped feature: each detector setup now carries its own **SL / T1 / T2**, editable from config, applied centrally by the engine. Replaces the old flat, purely-structural stops that had *opposite* problems per setup (exhaustion too tight ~0.6–4pt; oi_wall/breakout too wide ~30–48pt) — no single global knob could fix both.

**Design (decided via /grill-me before coding)**
- **Semantics**: SL and T1 are fixed absolute per-type distances from entry (replace structural). T2 = nearest structural level from `levels` beyond T1, else a per-type fallback distance.
- **Placement**: one engine step, `apply_per_type_levels(signal, settings, levels)`, runs in `tick()` right after the detector returns and **before the R:R gate** (so the gate sees final levels). Config-driven, keyed by `SetupType.value`.
- **Config shape**: `SetupLevels` dataclass + `per_type_levels` on `TuningConfig`, set in both profiles (identical for now; expiry may diverge after a month of live re-tuning).

**Shipped values** (validated, replace-semantics): EXHAUSTION 12/24/40 · CONTINUATION 25/40/80 · OI_WALL 12/25/40 · FAILED_BREAKOUT 15/30/55. R:R for all four ≥ 1.0 → nothing suppressed. Only EXHAUSTION (n=13) is well-supported; the other three (n=5/4/2) are indicative → **user re-tunes the 12 numbers after ~1 month**.

**Full cleanup (single source of truth)** — user asked to remove the old implementation:
- Detectors (all 4) no longer compute SL/T1/T2: removed the structural stop, structural/fixed target selection, T1/T2 ordering-swap and "Target set at structural…" reasons. They emit `0.0` placeholders; the engine fills them. Detection, direction, `option_type`, `entry_zone`, `strike` unchanged.
- Removed now-dead config knobs: `target_1_pts`, `target_2_pts`, `target_1_fallback_min_pts`, `target_2_fallback_min_pts` (kept `structural_target_min_distance_pts` — continuation scoring still uses it).
- Removed dead imports (breakout `Tuple`, options_math `Dict`, alerts `SetupType`) + 32 dead `mock_settings.target_*` fixture lines.

**Interaction surfaced honestly**: for the 4 configured types the R:R gate is now largely inert (per-type R:R ≥ 1 by construction) but still guards unconfigured/misconfigured entries — added a test proving it catches a misconfigured per-type entry (reward < risk).

**Validation**: TDD — new `tests/unit/test_per_type_levels.py` (20 tests: SL/T1 replace both directions, T2 nearest-level vs fallback, R:R passes, unknown-type safety, config pinned). Updated the detector / task175 / config tests to the new reality. **263 tests green**; 7 pre-existing failures unchanged (scratch async infra, test_analytics_qa, test_alerts). Net **+554 / −697** lines.

**Decisions**
- `per_type_levels` is the ONLY knob for trade levels; no structural fallback path remains.
- T2 stays structural-first (nearest level beyond T1); fixed per-type T2 applies only when no such level exists.
- Detectors detect; the engine owns trade levels — clean separation.

**Status**: **Merged (PR #32)**. POC/design record: [directives/TASK-185_pertype-config-POC.md](../directives/TASK-185_pertype-config-POC.md).

**TODOs**
- [ ] After ~1 month (≈2026-08) re-analyse live trades and adjust the per-type numbers.
- [ ] (Optional) sweep pre-existing unused imports in unrelated test files.

---

## 2026-07-09 · Silent-Detector Audit + Restore MEDIUM Breakouts (TASK-184)

Started from a live QA: "FailedBreakout / OIWall / Exhaustion feel silent — only TrendContinuation fires. By how much config margin did we miss?" Audited `ml_collection` (3,134 cycles, 06-29→07-09) plus a live Dhan chain pull.

**Findings**
- **Temporal flip on 07-07**: 06-29→07-06 the three faders fired 32× and continuation 0×; 07-07→07-09 the faders went to **0** and only continuation fired (matches the user's lived experience). Market data healthy both windows.
- **Breakout — config too tight (the fix target).** On 07-07 there were **10 genuine closed-back failed breakouts; 3 hit exactly score 2** and were all filtered by `breakout_failure_min_score = 3`. The missing 3rd point is usually `weak_volume`, which is anti-correlated with a real breakout. The `9ddb0cf` "working" baseline used `min_score = 2`.
- **OI-Wall — pre-existing design limit (not changed here).** `oi_wall_min_oi_change_pct = 5.0` is *per 60s cycle* and unchanged since the baseline; standing walls (live: 102-lakh PE @24000, 64-lakh CE @24100, both <80pt from spot) build only 0.3-1%/cycle → never re-qualify. Tracked for a future task.
- **Exhaustion — genuine regime.** Firing logic byte-for-byte unchanged since baseline; needs vol-climax ∧ doji simultaneously (anti-correlated), so trend days legitimately yield ~none (07-07→09 had 1 raw setup).

**Decision (TASK-184)** — full rationale in [directives/TASK-184](../directives/TASK-184_restore-medium-breakout.md). Lower `breakout_failure_min_score` **3→2** (both profiles) to restore the **MEDIUM-confidence** breakout tier. ARES is a **manual-trading confirmation aid**, not only an autotrader: a MEDIUM signal in the trader's direction is confidence, against it is a prompt to reconsider, and a silent detector gives neither. The `confidence` field already labels MEDIUM/HIGH. `closed_back` stays a hard gate; score 0-1 still never fires; R:R gate unchanged. **Intentional — not a bug; do not re-raise to 3.**

**Validation**: TDD — added `tests/unit/test_task184_medium_breakout.py` (4 tests, red→green). Rewrote the score-mechanic assertions in `test_task174_oi_scoring.py` / `test_task175_sl_config_obs.py` / `test_audit_p1_tuning.py` to check confidence + reason strings instead of fire-vs-None (they had encoded min_score=3). **250 tests green.** End-to-end replay: the real `FailedBreakoutDetector` with the new config fires **4 MEDIUM breakouts on 07-07** (min_score=3 baseline: 0).

**Honest caveat**: a forward P&L sim of the recovered 07-07 signals was net negative (~-47 pts, counter-trend fades in a down-drift); the live continuation trades that window also lost. Restoring MEDIUM raises signal *count*, not guaranteed profit — accepted by design (value = manual-management confirmation). Future task: trend-alignment guard so faders fire only into levels that hold.

**Status**: Implemented on `feature/TASK-184-restore-medium-breakout`. PR pending user review — **not merged** (user merges).

**TODOs**
- [ ] Open PR, user review, merge.
- [ ] Future task: fix OI-wall standing-wall blindness (absolute-magnitude or day-open baseline instead of per-cycle 5% build); it also revives the structural-level system feeding all detectors.
- [ ] Future task: trend-alignment guard for the faders (fade only into a level that holds) to lift fader P&L.

---

## 2026-07-08 11:30 · ML Offline Labeling & XGBoost Training Pipeline (TASK-183)

Started from a live QA of the ML side: "is `ml_collection` recording as expected, and does the collected data add value?" Audit findings (all verified against Supabase): collection itself is **healthy** — 2,669 rows, ~350/trading day, all 7 feature groups + `raw_candle` + `raw_atm_oi` fully populated. **But the data was inert**: (1) `ml_collection.trade_outcome`/`trade_pnl`/`trade_id` are **0/2669 non-null** — nothing ever back-fills them despite the schema comment; (2) the trainer (`data.py`, `source="ares"`) reads a *different* table, `trade_analytics` (38 rows, sparse features), and **never `ml_collection`**; (3) XGBoost was **dormant** — empty `models/`, `ml_predictions=0`. Net: rich features collected every minute, but unlabeled, unread, untrained. User directive: fix it so the data adds value, create ADR + task, and **leave the live implementation untouched**.

**Decisions** (full rationale in [ADR-183](../directives/ADR-183_ml-offline-labeling-training.md))
- **Label strategy = self-labeled forward-return, not trade outcomes.** Waiting for realized trades is hopeless (~1 trade/day now → months). Instead label **every** `ml_collection` row from its own forward price path: a **bidirectional forward-points triple-barrier** — from each candle, a clean ±`tp_points` (35) move before the opposing `sl_points` (25) stop within `lookforward` (5) candles → `1` ("tradeable move imminent", either direction), opposing stop first → `0`, neither (chop) → `-1` dropped. **Day-bounded** so a window never spans the 15:30→09:15 overnight gap (the existing `label_candle_forward` ignored this). Turns all 2,688 rows (+350/day) into training data *today*.
- **`ml_signal/dataset.py` (new)**: read-only `ml_collection` loader helpers — `flatten_features` (7 JSON groups → `group__key` numeric matrix, stable columns, missing→0.0, close from `raw_candle`), `label_forward_points`, `build_labeled_frame` (per-day labeling), `feature_columns`.
- **`ml_signal/train_offline.py` (new)**: `chronological_split` (leak-free, small-data-safe — the existing `trainer.train_pipeline` walk-forward needs 6mo+1mo = 7+ months → 0 folds on 2 weeks), `run_training` (XGBoost fit/eval/importance **inlined** with the same `MLConfig` params — `trainer.py` hard-imports `optuna`, an unneeded heavy dep, so it was left untouched rather than imported), and a `main()` CLI (`python -m ml_signal.train_offline`) that pulls `ml_collection`, labels, trains, and writes a model + JSON metrics report. Small-data guard: warns + marks metrics provisional, never crashes.
- **No DB writes, no schema changes, no new tables.** Pipeline reads `ml_collection` and writes only a model artifact (`ml_signal/models/`, gitignored) + `reports/ml/task183_offline_metrics.json`. Blast radius = disk only → zero risk to the live fly.io system.
- **Live inference deliberately deferred** to a future task, gated on a model that clears an AUC bar. Trade-outcome label fusion is future work too, once `trade_analytics` is large.

**Validation**: 13 new contract-level tests (`tests/unit/test_task183_ml_offline.py`) covering flatten, the forward-points labeler (win/loss/inconclusive/tail), day-boundary isolation, chronological split, and the small-data guard — fully synthetic, no live DB. **246 tests green** (233 existing untouched + 13 new). First live run on `ml_collection`: 2,688 rows → **168 labeled samples, pos-rate 0.393, AUC-ROC 0.547 (provisional)** — i.e. right at the 0.5 noise floor on ~2 weeks of mostly-flat data. Honest read: the machinery now works and XGBoost runs, but predictive value is **not proven yet**; rerun as trending/volatile days accumulate (a flat tape yields few conclusive labels — 2,520 of 2,688 rows were inconclusive chop). Top features by gain: volume-above-avg, dist-to-support, ATM total OI, OI bias, total vega.

**Status**: Implemented on `feature/TASK-183-ml-offline-labeling-training`. PR pending user review — **not merged** (user merges).

**TODOs**
- [ ] Open PR, user review, merge.
- [ ] Rerun `python -m ml_signal.train_offline` weekly; track whether AUC climbs above ~0.55 as data grows / trending days land.
- [ ] Future task: if a model clears the bar, wire live inference (`ml_predictions` / a pre-filter score) — separate PR, since that *does* touch the live path.
- [ ] Future: fuse realized trade outcomes (`trade_analytics`) as a second label signal once that table is large.
- [ ] Consider (not done): the `tp=35/sl=25/lookforward=5` label bar is strict on flat days; a looser bar would yield more labels but noisier ones — a knob to revisit with more data.

---

## 2026-07-07 16:00 · Remove the Observation Gate + Speed/IV-Crush Filters — Every Signal Trades (TASK-182)

The deferred follow-up flagged in TASK-180/181. Started from a live debug of "why only 1 trade today": pulled `ares_signals`/`trade_analytics`/`ml_collection` from Supabase and found the system had gone quiet not because detectors stopped firing but because the audit-era gates neutered them. Ground truth: 07-06 fired 8 signals, **all 8 forced observation-only** (1 breakout via Filter E's `trend_filter_enabled` counter-trend downgrade, 7 exhaustions via `exhaustion_alert_only`) — the runtime `reasons` strings named the exact gate on each. 07-07 was a clean trending session (regime held 43 candles) so only the trend-aligned continuation detector qualified, and it stopped out (−19 pts). The three fade detectors correctly found almost nothing to fade on a trending day. User's call: the paper P&L looked bad but they were profitable managing the trades by hand — the gates that "protect" by withholding trades were the regression. Remove the whole observation mechanism, and (scoping question answered explicitly: **Option 2**) the two other MEDIUM-suppressing filters too, keeping only the R:R sanity gate.

**Decisions**
- **`engine.py`**: deleted Filter A (flat-market speed filter), Filter B (anti-IV-crush), Filter D (`exhaustion_alert_only`), Filter D2 (`continuation_alert_only`) and Filter E (`trend_filter_enabled` counter-trend downgrade/suppression). The **R:R gate is the only remaining protective filter** — every fired setup is a live trade unless its risk:reward is degenerate. Dropped the `iv_lookback`/`pe_iv_lookback` buffers (existed only for Filter B) and the now-unused `Direction`/`SetupType` imports. Cooldown no longer has an `alert_only` carve-out.
- **`models.py`**: removed the `AresSignal.alert_only` field entirely — the observation concept no longer exists in the model.
- **`main.py`**: every fired signal now calls `position_manager.add_trade` unconditionally (dropped the `if signal.alert_only:` skip branch).
- **`alerts.py`**: removed the OBSERVATION-ONLY message formatting and the separate-channel routing (TASK-178) — one tradeable alert path.
- **`config_profiles.py` / `config.py` / `.env.example`**: deleted `exhaustion_alert_only`, `continuation_alert_only`, `trend_filter_enabled`, `speed_filter_window_candles`, `speed_filter_min_range_pts`, `iv_crush_lookback_size`, `iv_crush_percentile`, `iv_crush_min_samples`, and the `discord_observation_webhook_url` secret. **Kept** `continuation_enabled` (a real off-switch, unrelated to observation), the R:R gate, `time_stop_minutes`, cooldown and `tick_exit_check_interval_seconds`.
- **Detector logic verified unchanged vs the pre-observation baseline** (parent of TASK-171, 2026-07-02): breakout = cross+close-back, exhaustion = volume-climax+doji, OI-wall = approach+test+reject are all the same shape. What the audit changed *besides* the gates — stricter scoring (breakout min-score 2→3, writers 3%→10%) and SL sitting exactly at the structural level with no buffer (TASK-175, the reason the 07-07 continuation stopped at −19) — was left in place; not part of this ask.
- **Deploy note**: fly.io already runs latest `main`, so the flags were `False` in production before this; removing the machinery makes "trades always come through" structural rather than flag-dependent, so it can't silently regress again.

**Tests**: deleted `test_trend_regime_filter.py`, `test_task178_observation_discord_channel.py`, `test_engine_remediation.py` (all covered removed machinery). Reworked `test_audit_p1_tuning.py` (speed/IV-crush suppression → now-trades assertions), `test_engine_efficiency_gates.py` (exhaustion is tradeable, no `alert_only` attr), `test_task175_sl_config_obs.py` (observation-alert restyle → tradeable-alert), `test_task177_trend_continuation.py` and `test_continuation.py` (dropped `continuation_alert_only`/Filter-E cases, kept `continuation_enabled` off-switch coverage). **232 tests green.** End-to-end smoke: a MEDIUM exhaustion in a dead-flat, high-IV market now returns a live signal, sets the cooldown, and renders a normal "SIGNAL DETECTED" Discord card (no OBSERVATION).

**Follow-up (same branch/PR, user request):** re-added the speed filter's *condition* as a non-gating annotation. `speed_filter_window_candles`/`speed_filter_min_range_pts` are back in config, but the engine now only appends a `Price is FLAT — market moving under 15 points (last 15-candle range: X.X pts)` reason when the rolling range is sub-threshold — the signal still trades, it is just flagged in the Discord Reasons section. 233 tests green (+1: flat-note carried, trending-market no-note).

**Status**: Merged to `main` via [PR #28](https://github.com/dubeyshantanu2/ARES/pull/28). Local branch `feature/TASK-182-remove-observation-and-suppression-gates` deleted after merge.

**TODOs**
- [x] Open PR, user review, merge.
- [ ] Deploy is the user's own action (fly.io redeploy of `main` after merge).
- [ ] Watch live Discord: every detector's signals go straight to live trades now; the only thing that can withhold one is a degenerate R:R.
- [ ] Separate/unrelated: the 6 duplicate orphan OI_WALL `OPEN` rows at 10:10 on 07-06 (`signal_id=None`, never closed) point at a dedupe/logging defect in the position/analytics path — worth its own ticket.

---

## 2026-07-06 18:05 · Trend-Regime Filter Off by Default — All Signals Live (TASK-181)

Direct follow-up to TASK-180: once the exhaustion/continuation `alert_only` gates were live, the user saw Filter E (TASK-173's counter-trend downgrade/suppression) was still producing "OBSERVATION ONLY — NOT A TRADE" Discord cards for counter-trend HIGH-confidence signals from *any* detector (not just exhaustion), and suppressing counter-trend MEDIUM signals outright. User was unambiguous once this was explained: "we added a logic for observation trade logic which was coming in discord for last 2 days, i dont want that feature" — the whole observation-only mechanism, global scope, no per-detector carve-out. (Initially asked to confirm scope — global vs. exhaustion-only — since this removes the counter-trend protection for breakout/OI-wall too, not just exhaustion; user's clarification resolved that ambiguity in favor of the global option.)

**Decisions**
- **`config_profiles.py`**: `trend_filter_enabled` dataclass default flipped `True` → `False`. Single source of truth (same pattern as TASK-180's exhaustion/continuation consolidation, prompted by the earlier PR review comment) — no per-profile override needed since both profiles want the same value.
- **Filter E mechanism itself is untouched** in `engine.py` — same counter-trend downgrade/suppression code, just off by default. Fully testable by explicitly setting `trend_filter_enabled=True` via `dataclasses.replace()`, which is exactly what the existing mechanism tests now do.
- **Net effect**: with `exhaustion_alert_only=False`, `continuation_alert_only=False` (TASK-180) and now `trend_filter_enabled=False`, no signal from any of the 4 detectors can be tagged `alert_only` by any current mechanism — the observation-only Discord card (TASK-175/176/178) stops appearing entirely. The TASK-178 observation webhook routing code stays in place (harmless, unused unless something sets `alert_only` explicitly, e.g. future tests or a manually-triggered gate) — not removed, since the underlying capability may be wanted again later even if the auto-trigger is off.
- **Real risk accepted, explicitly**: breakout and OI-wall signals can now also fire live while counter-trend (fighting the current VWAP/PDH-PDL regime), not just exhaustion — this reintroduces exactly the risk class TASK-173 was built to gate. User's call, made with the tradeoff stated plainly beforehand.
- Tests updated: `test_trend_filter_enabled_defaults_false` (was `_true`), four counter-trend mechanism tests (`test_high_confidence_bullish_downgraded_in_downtrend`, `test_medium_confidence_bullish_suppressed_in_downtrend`, `test_high_confidence_bearish_downgraded_in_uptrend`, `test_medium_confidence_bearish_suppressed_in_uptrend`) now explicitly force `trend_filter_enabled=True` to keep exercising the mechanism; two new tests added for the live-by-default behavior (`test_counter_trend_high_passes_live_by_default`, `test_counter_trend_medium_passes_live_by_default`); `test_trend_filter_never_downgrades_aligned_continuation_signal` also forces the flag on so it still meaningfully proves continuation is exempt even when Filter E is active. 262 tests green (260 → 262).

**Status**: Merged to `main` via [PR #27](https://github.com/dubeyshantanu2/ARES/pull/27). Local branch `feature/TASK-181-disable-trend-filter-by-default` deleted after merge.

**TODOs**
- [x] Open PR, user review, merge.
- [ ] Deploy is the user's own action, as with prior tasks.
- [ ] Watch live Discord output post-deploy: every detector's signals should now go live regardless of trend alignment — no more observation-only cards from any of the 4 detectors under any current gate.

---

## 2026-07-06 17:15 · Exhaustion + Continuation Go Live (TASK-180)

User explicit call: no more time for observation-only signals — with the observation Discord channel already separated out (TASK-178), the `alert_only` safety gates on exhaustion and continuation no longer earn their keep as a "prove it before it trades" step. Flipped both to live, on both profiles, including expiry continuation (accepting that its pullback/resumption logic has zero expiry-day validation history — explicit user choice after being asked directly).

**Decisions**
- **`config_profiles.py`**: `exhaustion_alert_only=False` on both `NON_EXPIRY_CONFIG` and `EXPIRY_CONFIG`; `continuation_alert_only=False` on both; `continuation_enabled=True` on `EXPIRY_CONFIG` (previously `False` — TASK-177's expiry knobs, shorter regime/pullback windows and a higher score bar, were tuned in reserve and are now actually in effect). Dataclass field defaults on `TuningConfig` itself stay `True` — only the two profile instances changed — so any code path that constructs a bare `TuningConfig()` (existing tests, ad-hoc scripts) keeps the conservative default.
- **Important interaction, not a bug**: exhaustion is a reversal/fade detector — its signals are almost always counter-trend by construction. With `exhaustion_alert_only` no longer forcing `alert_only=True` upstream, Filter E (the trend-regime filter, TASK-173) now actually evaluates exhaustion signals for the first time: counter-trend HIGH confidence gets downgraded to observation-only, counter-trend MEDIUM gets suppressed outright. This is the exact same mechanism that motivated TASK-177 in the first place (a persistent trend blocks every counter-trend fade). Net effect: exhaustion signals are only live when they happen to align with the current VWAP/PDH-PDL regime — most won't, especially in a strong trend. Continuation signals are unaffected by this (trend-aligned by construction, Filter E never touches them per `test_trend_filter_never_downgrades_aligned_continuation_signal`).
- **Tests updated to stop depending on the flipped defaults for coverage of the underlying gate mechanisms** (which are unchanged) — `test_exhaustion_signal_is_tagged_alert_only_when_configured`, `test_continuation_alert_only_when_configured`, `test_already_alert_only_signal_skips_trend_check`, `test_observation_only_exhaustion_survives_iv_crush_filter` now explicitly force the relevant `_alert_only=True` override via `dataclasses.replace()` rather than relying on the profile default. New tests added for the now-default live behavior: `test_exhaustion_signal_is_live_by_default`, `test_continuation_consumes_cooldown_when_live_by_default`, `test_expiry_profile_runs_continuation_live`, `test_expiry_enables_live_continuation` (config test), plus `test_expiry_profile_disables_continuation_when_explicitly_off` to keep `continuation_enabled` covered as a real off-switch. 258 tests green (257 → 258, some renamed).

**Status**: Merged to `main` via [PR #26](https://github.com/dubeyshantanu2/ARES/pull/26). Local branch `feature/TASK-180-continuation-exhaustion-go-live` deleted after merge. 260 tests green (post-merge with TASK-179).

**Follow-up from PR review**: user asked why `exhaustion_alert_only`/`continuation_alert_only` were being set per-profile instead of once — consolidated to a single `TuningConfig` dataclass default (both profiles now inherit it, no duplication). User also confirmed intent directly on the PR ("from tomorrow I want the trade to come normally, not under observation") and separately clarified after seeing the Filter E caveat: **they don't want the Filter E counter-trend downgrade either** — "I want all the signals to come, like before" — this is a distinct, larger follow-up task (removing/loosening Filter E), explicitly deferred: "we will talk about it" after this PR merged.

**TODOs**
- [x] Open PR, user review, merge.
- [ ] Watch live Discord output for a few sessions: expect exhaustion signals to still mostly land in the observation channel during trending days (Filter E), and only fire live when aligned with the regime.
- [ ] Expiry continuation now live with zero real validation history — worth an early check on the first live expiry session.
- [ ] **Open follow-up**: user wants Filter E's counter-trend downgrade/suppression removed too, for "all signals like before" — scope not yet agreed (global `trend_filter_enabled=False` vs an exhaustion-only carve-out vs something else). Not started; needs a scoping conversation first given it reintroduces the exact counter-trend risk TASK-173 was built to gate.

---

## 2026-07-06 16:30 · Trend Continuation 2-Candle Resumption Confirmation (TASK-179)

Follow-up to the post-merge Dhan P&L exploration of TASK-177: user manually traced a real false signal — 2026-07-06 13:57, a BULL continuation entry that got stopped out 2 candles later — and found the cause: price had been declining for ~19 minutes straight (24458.65 high at 13:38 down to 24412.55 by 13:56), but a single 3-point up-close candle at 13:57 was enough to satisfy the resumption trigger (`close > open and close > vwap`) and fire the entry, even though the "trend" was really an unfinished slide, not a resumed rally. Explored two alternative fixes in scratchpad first — a dual-EMA(9/15) trend gate, both with and without a slope-angle filter — both back-tested substantially worse (net P&L went negative) because they lag too far behind price and cut good trades along with bad ones. The 2-candle confirmation (require the very next candle to also close in the trend direction) was the one that worked: same false trade removed, signal count 62→41, net P&L 108.0→123.7 pts, win rate 42%→63%, in an unscored scratch replay of the 23-session Dhan backtest window. TDD: `tests/unit/test_continuation.py` — updated 5 existing tests to feed the confirmation candle, added 2 new regression tests (`test_single_resumption_candle_does_not_fire`, `test_failed_second_candle_resets_pending_not_state`) — written before the implementation change. 257 tests green (255 → 257).

**Decisions**
- **New `ContinuationState.resume_pending` flag** (`detectors/continuation.py`): the first trend-aligned candle inside a pullback only arms the gate (`resume_pending = True`, no signal); the *next* candle must also close in the trend direction to fire — entry moves to that 2nd confirming candle's close/timestamp.
- **A failed confirmation doesn't reset the whole candidate** — only `resume_pending` clears; the pullback keeps tracking (extreme, candle count, timeout) so a later genuine 2-candle confirm inside the same pullback can still fire. Locked in by `test_failed_second_candle_resets_pending_not_state`.
- **Cost accepted**: entry is one candle later than before, so fill price is typically a few points worse on every trade that still fires — outweighed by the loser-count drop seen in the scratch replay (36→15 losers, same 26 winners retained).
- **Rejected alternatives** (scratchpad only, not implemented): dual-EMA(9/15) crossover as the resumption gate, with and without a slope>30°/-30° filter on both EMAs — both back-tested to a net loss (-60 to -66 pts vs the 1-candle baseline's +108) because the EMA relationship confirms too late, well after a lot of the real move has already happened.

**Status**: Merged to `main` via [PR #25](https://github.com/dubeyshantanu2/ARES/pull/25). Local branch `feature/TASK-179-two-candle-resumption-confirmation` deleted after merge.

**TODOs**
- [x] Open PR, user review, merge.
- [ ] The earlier arm-then-EMA(22)-trail exit exploration (still unimplemented, user said "I'll test it for some days") should be re-validated against this updated resumption logic before being considered for its own task.

---

## 2026-07-06 15:45 · Separate Discord Channel for Observation-Only Alerts (TASK-178)

User's main channel is getting spammed by observation-only alerts (exhaustion MEDIUM signals, the trend-filter-downgraded HIGH signals) — several of these can fire in a single session while tradeable signals are rare by design. Small, contained fix: route `alert_only` signals to a second, optional Discord webhook instead of the main one. TDD: `tests/unit/test_task178_observation_discord_channel.py` (5 tests) written before the implementation. 255 tests green (250 → 255).

**Decisions**
- **New optional secret** `discord_observation_webhook_url` (`config.py`), same convention as the existing `discord_health_webhook_url` — `None` default, `.env.example` documents it.
- **Routing in `send_discord`** (`alerts.py`): if the signal is `alert_only` *and* the observation webhook is configured, post there; otherwise fall back to the main webhook. Tradeable signals always use the main webhook, unconditionally.
- **Fallback is the safety net**: nothing changes for the current deployment until the user creates the Discord channel + webhook and sets the secret — no risk of silently dropping alerts if the new webhook is ever misconfigured or unreachable (same try/except-log pattern as the other alert senders).
- `send_trade_update` untouched — it only ever fires for tracked (tradeable) trades, since `alert_only` signals are never picked up by `PositionManager`.

**User action**: user created the Discord channel + webhook and added `DISCORD_OBSERVATION_WEBHOOK_URL` to the local `.env` (real webhook confirmed loading via `settings.discord_observation_webhook_url`). Fly secret + deploy (bundled with the still-pending TASK-177 deploy) is the user's own action, not run by the agent — Fly's depot builder was stalling on two prior attempts.

**Status**: Merged to `main` via [PR #24](https://github.com/dubeyshantanu2/ARES/pull/24). Local branch `feature/TASK-178-observation-discord-channel` deleted after merge. 255 tests green.

**TODOs**
- [x] Open PR, user review, merge.
- [x] User creates the Discord channel + webhook and sets the local `.env` secret.
- [ ] User sets the Fly secret and deploys (bundled with TASK-177) — user-owned, not run by the agent.

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
- [x] Audit `scratch/` for files still tracked in git from before the `.gitignore` change and remove them. Done 2026-07-20 — surfaced while wiring up CI, since a bare `pytest` collected them and failed 8 tests.

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
