# PR #147 comment disposition

Reviewed head: `a30ad6feadc1ca4637a39ccfe64027c223133d2d`.
Scope: address review feedback against the owner's agreed option ATR lifecycle; no production changes.

| Review comment | Relationship to agreed needs | Disposition |
|---|---|---|
| 4358075113: production option-candle wiring | The final 20% must actually receive premium protection after T2. | Already fixed in a30ad6f; verified real monitor/replay path; replied and resolved. |
| 4358075126: committed revisions | T1/T2 scores and OPEN runner state must remain consistent in Supabase. | Already fixed in a30ad6f; serial/recovery regressions verified; replied and resolved. |
| 4358075131: complete pinned contract | ATR must use the exact option without changing bracket or suggested lots. | Already fixed in a30ad6f; metadata/gate tests verified; replied and resolved. |
| 4358075138: fabricated index exit/PnL | Option close evidence cannot be substituted with an index stop price. | Already fixed in a30ad6f; nullable marks and allocation-weighted PnL verified; replied and resolved. |
| 4362614355: empty watermark outbox entries | Internal bar progress should not create undeliverable Discord events. | Fixed: only nonempty snapshots enter outbox; bounded silent retry identity stays server-owned. |
| 4362614362: Card A naive live timestamp | Discord must show the actual known IST signal time. | Fixed only at the initial card's live-candle boundary; aware conversions and strict durable event timestamps preserved. |
| 4362614346: auto-close at expiry | Adds an exit trigger and settlement/valuation policy beyond the agreed SL/ATR rules. | Owner explicitly deferred: “Keep agreed exits; handle expiry separately.” Not implemented or claimed fixed. Known boundary documented. |

## Validation

- TDD SQL regression failed on an empty `watermark-0` PENDING entry before the fix; the migration now passes **60 actual isolated PostgreSQL checks**. Twenty-four silent bars do not grow the outbox; null/missing/empty snapshots stay silent, real raises still queue, lost-response retry is idempotent, and stale client JSON cannot replace the server marker.
- Card A regression used `PriceFetcher.fetch_latest_candle`'s actual naive IST output. Initial red run: one failure (`Time: unavailable`). Focused alert/pricing/candle validation: **138 passed** after the narrow fix.
- Full frozen-code Python suite: **1,880 passed, 45 subtests passed**, 100.69 seconds, Python 3.14.6. Twelve existing dependency/deprecation warnings; no failures or skips.
- Production statement coverage: **89.07%** (8889/9980); branch coverage: **84.16%** (2949/3504). Both remain above the prior reviewed baseline and default pipeline thresholds; repository 100% target remains unmet.
- Independent agent reviewed the SQL implementation and caller retry contract: no blocking finding. Root reviewed Card A/source timezone behavior and all prior comment evidence.
- `git diff --check` and changed-source compilation passed.
- Correction commit and CI status are recorded in PR metadata after push. CI must be checked against that exact pushed head.

No initial pricing, lot sizing, risk calculation, milestone fractions, SL/ATR exit rules, production activation, migration application, deployment or broker order changed in this follow-up. No live test messages were sent; only explicitly requested GitHub review replies were posted.
