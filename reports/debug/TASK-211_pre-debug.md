# Pre-debug report — TASK-211

Date: 2026-10-09. Scope: first OI-wall watch Jev assessment.

Implementation follows the task directive and ADR. Application changes cover immutable observation/lifecycle identity, pure embed builders, ordered background persistence, fresh provider history and bounded confirmed-signal delivery fencing. Worker changes add watch-only context/questions, inference/delivery loops and service-role-only SQL lifecycle storage. Confirmed-signal context, Jev questions, formatter, engine, position manager and trade geometry remain unchanged.

Review-driven ADR refinements: partial-candle provenance requires refreshed provider history instead of aged engine-buffer snapshots; in-flight supersession requires a bounded delivery barrier and compensating closure for a late known acceptance; all four new tunables reside in `config_profiles.py` per user correction. No extra market-data requests or runtime dependencies introduced.

Tests were written at the public boundaries before implementation. Runtime test services use unroutable credentials and fakes; SQL tests run inside PGlite. No live Discord messages, trades, production migration or deployment performed.
