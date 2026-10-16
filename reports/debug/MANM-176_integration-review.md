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

## Retained boundaries
ATR trailing remains deferred. Existing spot outcome scores and carry semantics are preserved. Option P&L reports are estimates. Paper state is not a production broker ledger; live activation, carry protection and complete execution reconciliation require a later design. No production database changes, orders, messages or deployments were performed.
