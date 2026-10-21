# MANM-176 — Greeks-based option brackets

Date: 2026-10-16; ATR follow-up 2026-10-21
Status: PR #136 implemented; ATR and option-visibility follow-up in PR #143, pending review/merge
Workflow: Global Development Pipeline; Codex implementation/review; no Antigravity MCP.

## Goal
Implement ADR-176 Greeks-based option plans and the authorized post-T1 ATR runner on `codex/option-alert-visibility` (PR #143). Capital controls suggested lots only; execution rejection must not hide available calculated option data.

## Acceptance criteria
- Preserve side-specific broker identities, expiry, synchronized underlying quote, bid/ask and full Greeks for every option-chain strike.
- Select the valid contract nearest absolute delta 0.50; retain missing/nonfinite provider data without sentinel values; unverified capital yields zero executable lots while available prices remain visible.
- Build tick-rounded, strictly ordered Delta/Gamma brackets with diagnostic-only theta scenarios, nonnegative capped sizing and risk measured from the worst allowed entry to the executable stop.
- Dispatch explicit simulated limit orders by default; a submission is never treated as a fill. Validate protection and reconcile partial fills and invalidation through the order manager.
- Keep live broker calls behind an explicit adapter contract; tests never submit orders to an account.
- Persist final lots separately from uncapped suggestions and retain rejection diagnostics. Alerts distinguish estimated option P&L from broker fills.
- After T1, monitor completed five-minute spot closes against the previously active stop, then ratchet with entry/previous stop and Wilder ATR(5), HHV/LLV(10), multiplier 2.5. Preserve warmup, gap/session resets and restart watermarks; fixed T2 becomes reference only.
- Track every emitted, persisted signal as manual `SIGNAL_REFERENCE`, independent of capital/lot allocation/execution checks, without submitting new paper or broker entry orders; emit ATR stop-update/close-through alerts and preserve score 1 for post-T1 exits. Paper fills still require observed quotes.
- Verify focused regressions and the repository suite, complete review, update docs and CHANGELOG, and update PR #143 without merging.
- Retain failed manual-reference registrations for later-poll retry with their original payload and canonical ML linkage; preserve recovered lifecycle state. Keep the queue process-local, without new entry filters.

## Boundaries
ATR trailing is now implemented on this branch under ADR §4.3; the earlier deferral applied to PR #136. No detector geometry changes, new time stops, production database mutations, deployment or live account activation. Existing untracked research artifacts are unrelated and must remain untouched. Obsidian production sync follows the pipeline post-merge gate.

## References
- [ADR-176](adr/MANM-176_options-entry-exit-sl-greeks-limit-orders.md)
- [PR #136](https://github.com/Manmade-Anyme/ARES/pull/136)
- [PR #143](https://github.com/Manmade-Anyme/ARES/pull/143)

## Scope confirmation
On 2026-10-16 the user chose **Keep live activation blocked**. Live dispatch must reject before any broker submission; a later reviewed design will address broker lifecycle and carry/session protection.
