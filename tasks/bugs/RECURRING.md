# Recurring Bugs

## MANM-150 — Cross-layer persistence contracts

Repeated review regressions came from success-only mocks around async Supabase
writes and RPCs. Future persistence changes must assert schema-mode dispatch,
returned identity, complete payloads, conflict behavior, and an end-to-end
signal → active trade/analytics → ML join before handoff.
