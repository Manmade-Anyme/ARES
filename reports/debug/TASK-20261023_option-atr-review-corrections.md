# TASK-20261023 / PR #147 correction debug report

Date: 2026-10-23
Reviewed PR head: `6788dcb9dd392299d35cec994a072fd2278da23a`.
Base: `964566331d49037566228a8b7823de57476425a4`.

## Scope and result

R1–R14 are addressed on the existing PR branch. No initial index levels, Greeks option bracket, lot/risk calculations, detectors, or entry eligibility formulas changed. The candidate policy requires enabled configuration, exact parity version and backend credentials; defaults preserve legacy lifecycle management.

The implementation wires native pinned option history into the production monitor independently of index/chain failures. T1 seeds the previously confirmed line; completed premium closes compare the previous stop before any raise. Delayed source events, activation boundaries, in-session gaps, outages, partial allocations and nullable observed index marks have regression coverage.

Persistence now uses only the restricted transaction. Revision conflict recovery retains observed stops and terminal decisions. Durable immutable alerts commit before sending, claim transport before submission, retain acknowledgements across execution telemetry writes and restore terminal pending events independently of market hours. Ambiguous or in-flight submissions are not blindly retried.

## Independent review and corrections

Parallel runtime and storage/SQL/ML reviewers found additional issues during implementation: historical restoration cards before recovered exits, tick/REST chronology bypass, pre-T1 wicks hitting a newly armed guard, terminal conflict observations being discarded, stronger stops lost to a later milestone, stale T1 cards after conflict repair, acknowledgement/context races and terminal startup outbox starvation. Each was reproduced and corrected with targeted regressions. The final root check also rejected anonymous unkeyed option history and verified actual enrollment preserves original prices and lots.

The runtime reviewer reported no remaining blocker in reviewed paths after its final focused pass. The storage reviewer subsequently reached its usage limit; root verified its last reported stale-T1 correction with the conflict regression suite and re-ran the complete repository suite. This is not a claim of an additional independent review after that limit.

## Source replay

Both incident tests now use actual committed source candles through the real application monitor and PositionManager, with an explicit injected ledger adapter. #1950 reaches index T1 at the completed 10:09 source bar boundary (10:10 IST), T2 at the completed 10:15 boundary (10:16 IST), and retains its 20% runner at the fixture cutoff. #3935 uses 22500 PE / security 51331 / 27-Oct expiry and closes at the initial index SL at the 11:10 completed-bar boundary, score 0. These are source-resolution counterfactuals, not broker fills or tick-time reconstruction. No synthetic exit bar is inserted.

## Verification boundaries

See [the QA report](../qa/TASK-20261023_option-atr-review-corrections.md) for final test and coverage totals. The real isolated PostgreSQL engine passes 29 migration/role/claim/rollback checks. Python lifecycle tests inject an explicit ledger; they do not substitute independent table writes in production.

Exact Dia indicator/feed parity remains unverified. The current-session replay is not chart-equivalent prior-session warmup. Production SQL, carried-trade changes, activation, deployment, restarts, broker orders and live Discord test messages were not performed. The offline migration preview produces no writes and requires sourced registration/history, schema evidence, parity evidence and human approval. Exchange session status is confirmed from source data or trade registration; a cold start with all providers unavailable remains unknown/log-only rather than falsely declaring a holiday outage.
