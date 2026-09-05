# TASK-073-R1 — Replay evidence audit and repair contract

Task: MANM-73 (`01a032c1-b430-700c-acc0-a3681a5c6c5e`). Date: 2026-09-05.
Author: Software Architect. Status: proposed replay-method clarification for human review through Project Manager; not implementation or release approval.

## Outcome

PR #103 is not yet supported by a valid historical replay. The immediate import failure is reproducible and straightforward to repair, but adding three report columns will not fix the underlying evidence. The runner substitutes invented candles and a permanently persistent wall, bypasses the real engine, and uses different targets and exit rules. The supplied fixture lacks the strike-level OI history needed to verify persistence. No production code or tests were changed in this architect intervention.

The remedy is a bounded replay repair through the Engineering Delivery Graph, not manual implementation by the user or another unilateral implementation by Mika. Project Manager owns routing and any approval/retry transition. The original entry design was human-approved through PR #102; its replay and live-release gates still stand. This proposal neither resets the graph's failed-attempt counter nor clears `human_gate` automatically.

## 1. Pinned evidence and reproduction

- Reviewed [PR #103](https://github.com/Manmade-Anyme/ARES/pull/103) at `346cfb692c58a6f265c91786f98833d8208c5d36`.
- Original ADR approved through [PR #102](https://github.com/Manmade-Anyme/ARES/pull/102), merged at `575df8c4f915117f861d9ccda6bbf45cd3a11b6b`.
- Fixture: `tests/fixtures/task073_18_trades_replay.json`; SHA-256 `deb487329a255baec729fa1207fddf9a46acdde6bc57a2d8d0c6e37b1a2bc3bd`.
- Discovery: `ai-grep search replay_task073` selected the runner, fixture test and report. AI-Grep queries `extract_candle_features` and `sl_points_oi_wall` returned no matches; targeted `rg` over their known dependencies located `ml_signal/features.py`, `ml_signal/collector.py`, `config_profiles.py`, `engine.py`, `main.py` and `position_manager.py`. No repository-wide content scan.
- Python 3.14.6: `env -u PYTHONPATH python3 scripts/replay_task073.py` fails at line 4 with `ModuleNotFoundError: No module named 'models'`.
- `python3 -m pytest tests/unit -k 'not test_18_trade_replay_execution' -q`: **421 passed, 1 deselected, 8 subtests passed, 1 XGBoost serialization warning**. The deselected test overwrites the tracked report and only checks its title and an asserted-policy sentence; it does not validate replay correctness.
- Executed existing `run_replay()` with its report writer intercepted in memory. It reports historical 18 entries / 13 SL / -127.2 points and synthetic Phase 1 4 qualifications / 4 SL / -64.0 points. These are reproducibility observations, not validated performance metrics. The tracked report was not rewritten.
- Codex findings about same-candle re-test and missing wall-expiration telemetry already have fix replies linked to `4eb2144` in the PR threads. Targeted filter/engine/replay tests, excluding report writing, pass: 20 passed, 1 deselected. This is not a blanket approval of PR #103.

## 2. Confirmed causes

| Area | Observed at the pinned revision | Required correction |
|---|---|---|
| Candle fidelity | `scripts/replay_task073.py:110` uses open=close=spot, high=spot+2, low=spot-2, volume=1000; captured features are ignored. | Prefer actual closed OHLCV; never substitute geometry without labelling the run synthetic. |
| Wall fidelity | Lines 90–107 inject OI=1,000,000, growth=15%, percentile=95, count=3, duration=180 on every observation. Detector thresholds are not exercised. | Replay observed chain inputs through `OIWallDetector`; persistence must accumulate/reset, not be pre-awarded. |
| Wall reference | All 18 `wall_price` fields equal `entry_price`. No strike series is supplied. | Recover actual wall strikes from source evidence; entry spot is not a valid fallback for strike identity. |
| Missing inputs | All 1,227 trajectory rows contain only timestamp, spot, JSON-string candle features and JSON-string aggregate/ATM OI features. No complete strike-keyed chain, candle timestamp, or engine context is included. | Validate provenance and completeness before classifying a run as historical replay. |
| Signal lifecycle | Line 126 counts `QUALIFIED` as a trade; it never invokes engine arbitration, R:R, per-type levels or acknowledgement. Filter state resets separately for each historical trade. | Count only real engine emissions; preserve session state and deduplicate overlapping windows. |
| Risk policy | Lines 138–140 read three nonexistent global aliases. Defaults silently produce SL/T1/T2=16/20/40; the actual OI profile is 16/25/40 with structural T2 resolution. Four recorded baselines used 12-point SL, fourteen used 16. | Use central per-type policy. Separate recorded outcomes from a same-policy comparison. Do not rewrite historical levels to claim they were 16. |
| Exit policy | Runner checks spot only, SL before targets, ignores existing T1-to-BE/time-stop behavior, and calls an arbitrary final sample `EOD_EXIT`. Substring classification (`"T" in result`) also matches `EOD_EXIT`. | Use explicit event semantics and enum membership; retain production lifecycle rules and label incomplete observation as censored. |
| Coverage | Fixture trade 15 ends at 04:51:44 UTC although its recorded exit is 05:59:03 UTC; trades 2 and 3 share overlapping July 27 observations. | Report missing outcome coverage and overlap, not full execution-window coverage. Never replay duplicated observations as independent persistence. |
| Reporting | Missing T1 capture rate, median time-to-SL and average R:R. Report equates 14 unqualified cases with avoided shakeouts. | Return structured metrics with denominators, unknowns and coverage; no automatic success narrative. |

The collector stores `raw_candle` and `raw_atm_oi` separately (`ml_signal/collector.py:239`), but not complete strike-level chain history. The runner fixture omits both raw fields. An authorized existing-data export may recover OHLCV; it must not be described as recovering full-chain history unless that history actually exists elsewhere. No production database or broker was queried in this review.

## 3. Decision and boundaries

Repair the existing runner, fixture contract and regression tests. Do not add a general backtesting service, retune filters, widen initial SL, change production exit ordering, change live settings, or add a broker/data dependency to the evaluation path.

Performance/security: bound evaluation to the declared sessions and replay each unique observation once; sort/deduplicate the small input offline, never in the live loop. Validate finite numeric values, timestamp ordering and OHLC consistency before execution. Export only task-relevant market observations and opaque row IDs, excluding credentials and unrelated account data; all external I/O stays disabled during replay and tests.

Keep three evidence categories visibly separate:

1. **Recorded baseline:** immutable observed trade IDs, entries, original risk levels, exits and recorded P&L. Reconcile the selected IDs against the original 18 reviewed trades; the ADR's earlier 12-SL observation and this fixture's 13-SL count are not silently interchangeable.
2. **Controlled baseline and Phase 1:** replay the pre-Phase-1 and candidate paths with the same valid observations, selected profile, central risk/exit policy, clock and analysis window. Pin both revisions and configuration. Only the entry behavior differs. Recorded mixed-policy P&L is not the controlled comparator.
3. **Synthetic diagnostics:** useful deterministic filter/metric tests. Never called production replay, never counted as historical acceptance evidence.

Replay flow: validated session observations → engine at replay time → emitted signals → existing position lifecycle with external I/O replaced by offline adapters → event ledger → six metrics plus coverage report.

Run one engine/filter state per session, with sufficient earlier observations to reconstruct warmup, cooldown and consumed-wall state. Merge duplicate timestamps only when source identity/payload agree; conflicting duplicates are errors. Preserve all 18 cohort IDs and report their dispositions. Report unique replay emissions separately, including additional or shared emissions; do not force one new entry per old trade or use 18 as every denominator.

Reusing `PositionManager.update_trades()` requires isolated in-memory storage/alert adapters and a replay clock. Its present target-first intrabar convention, T1-to-entry stop, time-stop behavior and P&L accounting remain unchanged and must be disclosed. Do not introduce a competing SL-first simulator to make the report easier. Existing engine wall-clock cooldown and position time-stop checks must use observation time during replay without contacting live services. Do not assess an entry against a candle range that occurred before that entry; mirror the production evaluation order.

## 4. Input and API contract for Code Generator

Keep the existing default runner entry point compatible:

- `run_replay(fixture_path: Optional[Path] = None, output_dir: Optional[Path] = None) -> dict`: default paths resolved relative to the repository containing the script, not the caller's working directory. Return structured results; write Markdown/JSON only to the requested output destination.
- `validate_fixture(payload: object) -> dict`: return `status`, per-case missing fields/coverage reasons, cohort IDs and source/configuration provenance. Malformed input is an error, not an empty successful report.
- CLI supports explicit `--fixture` and `--output-dir`; works as both `python3 scripts/replay_task073.py` and `python3 -m scripts.replay_task073` without `PYTHONPATH`. Missing/invalid input and incomplete historical acceptance evidence exit nonzero with an actionable diagnostic. Synthetic mode, if retained, is explicit and cannot output an acceptance PASS.

The minimum versioned input envelope identifies source export/hash, cohort trade IDs, baseline/candidate revisions, configured profile (including expiry selection), session/timezone and evaluation horizon. Each observation carries:

- source row/snapshot identity, observed-at timestamp and the actual closed candle's timestamp;
- actual OHLCV (and nullable VWAP), spot and the engine's ATM/IV inputs;
- complete strike-keyed CE/PE OI and OI-change observations used by wall selection, plus levels and any other inputs used by the actual engine path;
- a declaration of missing observations and the source's coverage/granularity, not an assumed full session.

Normalize JSON-string feature fields once, retaining their captured values for audit. `compute_candle_features` uses candle **close**, not the separately collected spot, as its percentage denominator. Feature inversion is permissible only if the actual matching close and feature-producer provenance are available: recover signed body/open and wick extrema from those exact definitions, label them reconstructed, and validate range/ratio consistency. Without a proven close/spot equivalence, setting close=spot is not a faithful reconstruction. Prefer `raw_candle` export.

OI totals, PCR, maxima and percentiles cannot identify the strike-to-OI mapping or its changes over time. ATM OI is not evidence for a non-ATM wall. Do not reconstruct a chain from these aggregates, hardcode persistence, round entry spot to a strike, or backfill future wall observations into earlier candles. Missing required inputs produce `INCOMPLETE_INPUT`, with per-case reasons; they are not `UNQUALIFIED` trades. If no historical chain archive exists, PM must present that specific data limitation to the human and seek a separately approved evidence/collection plan. New capture cannot retroactively validate these 18 cases.

## 5. Metric contract

The result contains `schema_version`, `evidence_kind`, `input_status`, provenance/configuration, `cohort_dispositions`, `events`, and `metrics` for each comparison arm. Each metric includes value, unit, numerator/denominator or sample count, and an unavailable reason when null. Zero entries, zero SL events, missing inputs and censored exits are distinct conditions.

For an arm with complete comparable outcome coverage and N unique emitted entries:

| Required metric | Definition |
|---|---|
| Entry count | N engine-returned OI-wall signals after all gates and `EMITTED` acknowledgement; retain actual accepted-position/fill count separately if the position layer rejects duplicates. |
| SL-hit rate | Number with final `SL_HIT` / N. BE and time-stop exits are separate categories. If no entries, null with denominator 0. |
| T1 capture rate | Number that reaches T1 or a farther target before final exit / N. Count once per trade, including T1 followed by BE; a T2 event implies T1 capture even if no separate T1 event was recorded. |
| Median time-to-SL | Median elapsed seconds from actual entry to initial-SL event among `SL_HIT` trades only. No SL events means null, not 0. |
| Average R:R | Arithmetic mean of `abs(target_1 - trigger_price) / abs(initial_stop - trigger_price)` for emitted signals, matching the current engine gate. Retain a separately named T2 R:R if useful; do not call realized P&L/R the entry R:R. |
| P&L | Sum closed-trade spot-point P&L under the same pinned production accounting convention in both controlled arms. Preserve recorded P&L separately. No claim about option premiums, fees or realized account returns. |

Expose censored positions and coverage counts alongside the metrics. With incomplete follow-through, primary comparison rates/P&L are unavailable as final acceptance evidence; any observed-to-cutoff metrics must be separately labelled partial. An arbitrary last observation is not EOD and cannot close an open trade as though the exit occurred. A defined common cutoff may carry unrealized mark-to-market separately, never mixed into realized P&L.

Fixed-policy proof comes from values, not a report sentence: for every emitted signal assert initial stop distance equals the active `per_type_levels["OI_WALL_REJECTION"].stop_pts`; assert T1 distance and centrally resolved T2 too. At this revision both profiles use 16-point initial SL and 25-point T1. Preserve existing post-T1/time-stop tightening; “unchanged initial SL” does not mean disabling the existing lifecycle.

## 6. File assignments and executable acceptance checklist

Superseding human direction on 2026-09-05 retired the 18-trade historical replay
as a release gate because ARES has no equivalent historical replay requirement
for breakout, continuation, exhaustion, or expiry-detector trade paths. The
analysis below remains useful as an audit of why the previous replay artifact was
invalid, but Code Generator should remove the replay runner and fixture from the
release gate rather than repairing them for acceptance.

Project Manager routes the following; Architect does not implement these files:

| Owner through PM | Files / responsibility |
|---|---|
| Code Generator | Remove `scripts/replay_task073.py` and `tests/fixtures/task073_18_trades_replay.json` as release-gate artifacts; keep any future diagnostics separate from acceptance evidence. |
| Code Generator | Cover TASK-073 with focused unit and engine integration tests against existing detector/risk/cooldown/priority/acknowledgement behavior. |
| Documentation | Delete `reports/replays/task073_18_trades_replay_report.md`; do not replace it with a success report until a separately approved evidence plan exists. |
| PR Reviewer + QA | Independently verify unchanged risk, existing detector behavior, entry-filter regression coverage, and live-network isolation at the resulting SHA. |

Acceptance tests must check behavior and values, not only report headings:

- CLI subprocess from repository root and another working directory, with `PYTHONPATH` unset and explicit temporary output; missing fixture fails clearly; repeated runs are deterministic.
- Captured candle highs/lows change outcomes as expected; a missing/weak/disappearing wall cannot persist or emit; insufficient OI evidence fails validation instead of silently using defaults.
- Same-session overlapping samples do not duplicate persistence or emissions; cooldown, priority, R:R and acknowledgement remain active. Both direction mirrors and both profiles are covered.
- Emitted initial SL/T1/T2 match the real central configuration; historical 12-point stops remain labelled historical, not silently converted.
- Independently calculable event-ledger examples cover zero entries, no SL, T1→BE, T2, time-stop, ambiguous intrabar touches under the pinned production convention, and censored trajectories. Assert all six values and denominators; keep synthetic examples separate from the historical report.
- Every original cohort ID is retained with a reasoned disposition; incomplete data never counts as a successfully filtered loss. Report source hashes, revisions, unique observation counts and coverage.
- No broker, database or Discord access; do not import/start the live application. No tracked report modification during unit tests.

## 7. Alternatives and gate

- **Add missing headings only:** rejected; preserves synthetic inputs and gives false assurance.
- **Treat aggregate OI or rounded entry spot as wall history:** rejected; the reconstruction is not identifiable from the captured data.
- **Build a new backtesting platform / retune risk:** rejected; beyond this issue and unnecessary for a bounded offline runner.
- **Repair evidence honestly using existing components:** selected. Small runner changes can proceed after the appropriate PM/human review; archival data completeness may remain a genuine external dependency.

Definition of done for the repair: reproducible runner and value-based regressions, authentic input provenance and complete comparable coverage for the agreed cohort, all six metrics, unchanged risk/lifecycle proof, Documentation and independent Review/QA evidence. Numerical rollout thresholds were never specified in the approved ADR; PM must obtain human agreement on them before live activation, without optimizing them after seeing results. Do not mark Stage 2 or live rollout accepted merely because unit tests pass or fewer trades are emitted.

No initial stop widening, production edits, worker dispatch, metadata reset, merge or deployment is authorized by this document alone. The architect investigation is complete; implementation/replay validation remains open on PR #103.
