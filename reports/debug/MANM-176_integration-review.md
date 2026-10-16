# MANM-176 — Integration review

Date: 2026-10-16
Review: Codex math/integration reviewer and primary architect.

## Findings resolved
- Rejected sizing formatted null premiums: use a None-safe audit summary.
- Trade registration discarded rich options metadata: preserve Greeks, IDs, lot size, planned/final quantities and rejection reason.
- Submission could become a phantom position: require observed executable paper ask and protected fill.
- Duplicate pending signals could fill before active-trade dedupe: deduplicate pending plans before dispatch.
- One registration error stopped all later order/market work: isolate failures per pending record, retain retry state.
- Execution transitions were memory-only: persist updates to trade analytics while preserving existing context.
- Delayed fills lost ML outcome linkage: bind only unbound rows matching canonical signal UUID, retry until snapshot exists.
- Pre-fill candle extremes could close delayed entries: use current spot for a candle predating paper fill.
- Nearest-tick rounding could exceed entry cap: floor the entry tick when necessary.
- Paper premium targets were dependent on spot touches: use observed bid limits as well as synthetic spot exits.
- Crossed quotes could fabricate fills: reject crossed execution quotes.
- Live manager could submit without complete exit management: reject before broker call, per user scope confirmation.

## PR review follow-up — 2026-10-16
- [4291643658](https://github.com/Manmade-Anyme/ARES/pull/136#discussion_r4291643658): Paper execution previously consumed the candle spot alongside a newer option quote. The matched contract row's finite positive `underlying_spot` now drives the fill reference, persisted `spot_at_fill`, analytics entry and post-T1 break-even. Invalid or missing quote spot prevents quote execution while independent spot invalidation remains active.
- [4291668986](https://github.com/Manmade-Anyme/ARES/pull/136#discussion_r4291668986): A same-quote fill and emergency exit previously bypassed registration. Protected fills now remain registrable in `CLOSED` and `EXIT_PENDING`, with original fill metadata retained across retries. The regression with ask 99, bid 94, stop 95 and quantity 130 persists the closed execution and its paper loss of 650.
- `sync_execution()` reports incomplete synchronization until the canonical ML snapshot exists and its trade link is confirmed. Filled terminal records remain available for retry after main inserts that snapshot. These records no longer participate in pending-entry deduplication. Both bridge and greenfield schema paths retain their canonical signal guards.
- Focused verification: 211 tests passed; `options_math`, `execution.order_manager` and `execution.signal_dispatch` retain 100% statement and branch coverage. Full-suite results are recorded separately in the QA report after completion.

## Retained boundaries
ATR trailing remains deferred. Existing spot outcome scores and carry semantics are preserved. Option P&L reports are estimates. Paper state is not a production broker ledger; live activation, carry protection and complete execution reconciliation require a later design. No production database changes, orders, messages or deployments were performed.
