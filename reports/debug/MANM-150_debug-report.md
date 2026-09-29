# Debug Report — MANM-150

**Verdict:** CLEAN after fixes

## Reproduced regressions

- `Storage.log_signal` returned `None` and omitted canonical UUID/display fields.
- Runtime always selected the bridge RPC, including after the cutover migration.
- Atomic trade entry discarded the supplied ATM OI context.
- UUID5 trade IDs made valid re-entry conflict with a previous trade.
- Conflict retries accepted stale database rows without validating their values.
- Every routine ML snapshot blocked exit processing on remote persistence.
- ML snapshots omitted canonical signal UUID, display ID, trade ID, and binding status.
- Persisted trade alerts exposed the canonical key instead of the display ID.

## Root cause

The async and RPC conversion was tested with success-only mocks that did not
assert database function selection, returned identities, payload fidelity, or
the end-to-end join contract. The implementation therefore stopped between
layers while the existing unit suite remained green.

## Verification

- Focused regression suite: 116 passed, 8 subtests passed.
- Full suite: 532 passed, 8 subtests passed.
- Changed production files and new regression suite pass Ruff.
- No debug instrumentation or historical data mutation was introduced.
