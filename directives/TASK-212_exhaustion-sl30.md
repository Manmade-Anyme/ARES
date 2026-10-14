# TASK-212 — Exhaustion Stop at 30 Points

Date: 2026-10-14
Status: implementation authorized; pending PR review

Set exhaustion SL to 30 points with T1 18 and the existing structural T2 / 40-point fallback. Configure minimum T1 reward/risk at 0.60 for exhaustion only; other setups keep the global 1.0 minimum. Apply through the existing expiry and non-expiry tables.

The user explicitly requested minimum configuration changes and no extra code. Acceptance: only the existing configuration and gate change at runtime; no runner, profile factory, database migration, detector changes or new dependency. Verify both directions, exact threshold acceptance, rejection below the threshold and unchanged other-setup behavior. Update existing tests and documentation, then prepare one PR under the Global Development Pipeline. No merge or deployment in this task.
