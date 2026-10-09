# TASK-211 — Jev in the first OI-wall ready-watch message

Date: 2026-10-08; verified 2026-10-09. Status: implemented and locally verified; PR review and production rollout pending. Model: GPT-6 Codex, advanced reasoning for design; delegated implementation and review use the inherited model.

## Goal

Append exactly one Jev prediction field to the first OI_WALL_RETEST_READY Discord embed. Preserve the original fields, guidance, title and footer, and leave confirmed-trade T1/SL/T2 Jev inference unchanged.

## Acceptance criteria

- One combined first watch message, never a separate watch Jev follow-up.
- Watch-specific Noul closing probability, Choice outlook, and three independent Score evidence judgments use an immutable contemporaneous snapshot.
- Closing threshold is watch spot minus/plus a configurable 25-point minimum directional move. Display actual threshold and minimum move; no option-profit claim.
- Missing and unavailable history stays explicit. No later candles, incomplete opening-range labels, fictitious VWAP or private account data in the context.
- Independent Jev worker invokes inference; routine watch observation dispatch never waits for inference or delivery. Confirmed OI-signal publication uses bounded persistence/in-flight transport fences to preserve notification order, with explicit degraded-ordering fallback.
- Durable watch outbox with event UUID, exact input/payload, timestamps, version/model metadata, single inference claim and single delivery owner.
- Default inference-wait budget 8 seconds, total delivery freshness 60 seconds. Deadline/error produces an unavailable field in the same original message. Canceled/replaced/consumed watches cannot be sent late. Already accepted watch followed by cancellation retains existing cancellation format.
- Ambiguous webhook outcomes are archived UNKNOWN and not blindly resent. Explicit retryable HTTP responses can retry while fresh. Restart recovery cannot replay previously accepted messages.
- Before either watch loop polls, recovery must complete for the launcher's shared producer UUID and UTC start time, regardless of application/consumer scheduling order. Consumer-only restart preserves current-run watches; absent/invalid metadata or recovery failure preserves confirmed-trade Jev. Without valid shared metadata, the application retains direct watch alerts rather than queueing watches without a coordinated delivery worker.
- Database active-producer fencing rejects delayed prior-run enqueues, claims and recovery attempts after a newer run takes ownership. Ordering uses the fixed trusted launcher start time. Concurrent enqueue/recovery must serialize; cancellation and result/transport archival remain available for retired watches.
- While the application runs, the launcher's existing monitor cycle replaces an exited consumer with the same frozen run metadata. Application execution continues, and shutdown cleans up the replacement. Verify with actual subprocesses, without live database or Discord access.
- Production credential isolation in tests; SQL lifecycle tests run in isolated PostgreSQL/PGlite. Docs and rollout instructions included. No live Discord messages during verification.

## Authorized scope

The prior analysis and current implementation instruction authorize this watch-specific addition, including necessary persistence and asynchronous notification plumbing. No changes to entry qualification, SL, targets, quantities or trade management. Existing TASK-210 separate-message requirement is overridden only for ready-watch events.

The user's 2026-10-09 correction places all four new ready-watch settings in shared `OI_WATCH_JEV_CONFIG` in `config_profiles.py`. Environment files contain credentials only; old watch configuration environment variables are ignored.
