# QA Report — TASK-20261030 Weekly Option Reference

**Verdict:** PASS for the implemented path and its local validations. Production capture and historical recovery remain gated.

## Verification

| Check | Result |
|---|---|
| Full Python suite | 2,122 passed, 45 subtests passed, 12 warnings |
| Focused weekly backfill, storage, and report unit tests | 36 passed |
| Isolated PGlite migration/RPC integration | Passed, including permission, exact-cohort, expiry-code, idempotency, and no-partial-write checks |
| `py_compile`, `node --check`, and `git diff --check` | Passed |
| Read-only Dhan/Supabase dry run for 26–30 Oct 2026 | 11 of 16 pinned-contract entry/exit pairs verified; report preview correctly leaves option totals unavailable |

The SQL integration test removes `sampled_at` and `volume` from candidate evidence, checks that each request is rejected, and verifies that no cohort rows were written. The full Python run also exposed a fixed-date test fixture that expired during the run; its three option-expiry fixtures now use a relative future date, and the rerun passed.

## Production boundary

No backfill was applied, no capture flag was enabled, and no Discord message was sent during QA or feature implementation; the earlier manually delivered weekly report remains unchanged. Current evidence covers only 11/16 trades; the all-or-nothing recovery RPC refuses that partial cohort. Apply the migration only after PR #151 is merged, its SHA is verified, and migration approval is given separately. Future capture also requires separate activation approval. Historical recovery still requires verifiable marks for all 16 trades.
