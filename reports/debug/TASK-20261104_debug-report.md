# Debug Report — PR #156 Terminal Paper Exit Recovery

**Verdict:** CLEAN — the P1 restart gap is addressed in the branch; live service and broker behavior were not exercised.

## Finding and cause

The option execution retry monitor previously considered active trades only. After restart, a reference trade that had become terminal could therefore lose its in-memory route intent and paper-order binding. An accepted route marker also did not by itself keep a paper execution in `EXIT_PENDING` connected to quote monitoring, so an exit awaiting observed bid confirmation could be stranded.

## Fix

- Restore pending terminal route records from persisted analytics context and retry their terminal-safe route acknowledgement.
- Persist a complete paper `OrderRecord` snapshot with execution telemetry, and restore the signal/order binding only after validating signal identity, PAPER mode, confirmed fill, status, expiry, strike, security ID, exchange, and option type. A validated legacy snapshot fallback remains supported.
- Keep terminal paper executions with `EXIT_PENDING` on the routing and quote-monitor path after acknowledgement. Clear the route queue only when acknowledgement succeeds and no pending paper exit remains.
- Fail closed on malformed or mismatched state; no live execution is restored from this recovery path.

## Evidence boundary

The restart regression tests cover restored legacy and full snapshots, route acknowledgement, resumed bid processing, and malformed or unavailable persisted state. Local tests do not prove a production restart, database RPC behavior under live service conditions, broker fills, or trading outcome.
