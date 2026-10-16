# MANM-176 — Greeks-based option brackets

Date: 2026-10-16
Status: implemented and locally verified; PR #136 update
Workflow: Global Development Pipeline; Codex implementation/review; no Antigravity MCP.

## Goal
Implement the active scope of ADR-176 on `feature/MANM-176-options-entry-exit-sl`, preserving the existing spot outcome taxonomy and post-T1 break-even behavior.

## Acceptance criteria
- Preserve side-specific broker identities, expiry, synchronized underlying quote, bid/ask and full Greeks for every option-chain strike.
- Select the valid contract nearest absolute delta 0.50; reject missing/nonfinite quotes and unverified capital without sentinel values.
- Build tick-rounded, strictly ordered Delta/Gamma brackets with theta feasibility, nonnegative capped sizing and risk measured from the worst allowed entry to the executable stop.
- Dispatch explicit simulated limit orders by default; a submission is never treated as a fill. Validate protection and reconcile partial fills and invalidation through the order manager.
- Keep live broker calls behind an explicit adapter contract; tests never submit orders to an account.
- Persist final lots separately from uncapped suggestions and retain rejection diagnostics. Alerts distinguish estimated option P&L from broker fills.
- Verify focused regressions and the repository suite, complete review, update docs and CHANGELOG, and push to PR #136 without merging.

## Boundaries
ATR trailing is deferred by the latest ADR scope note. No detector geometry changes, new time stops, production database mutations, deployment or live account activation. Existing untracked research artifacts are unrelated and must remain untouched. Obsidian production sync follows the pipeline post-merge gate.

## References
- [ADR-176](adr/MANM-176_options-entry-exit-sl-greeks-limit-orders.md)
- [PR #136](https://github.com/Manmade-Anyme/ARES/pull/136)

## Scope confirmation
On 2026-10-16 the user chose **Keep live activation blocked**. Live dispatch must reject before any broker submission; a later reviewed design will address broker lifecycle and carry/session protection.
