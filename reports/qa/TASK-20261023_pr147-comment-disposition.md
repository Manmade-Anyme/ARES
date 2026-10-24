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

## Cloud-task follow-up: newer T2 versus stronger committed stop

Follow-up starting head: `f958e838acb7038337fb7d7802354ec4b2a26f38`. The cloud task [reported a fix](https://github.com/Manmade-Anyme/ARES/pull/147#issuecomment-6374383435) under commit `ba7706e`, but that commit was unavailable from the ARES repository during recovery. This follow-up recreates the scoped fix locally on the existing PR branch; it does not claim to recover or cherry-pick the unpublished cloud commit.

The finding relates directly to the agreed lifecycle: a revision conflict must not discard an observed T2 touch because a concurrent T1 context already has a higher locked stop. The correction retains the T2 event, score 2, 80% model-booked allocation, 20% remaining runner and disabled index guard, while carrying forward the stronger committed stop and protection mode into live state and the immutable T2 alert. Original event identity remains stable for retry deduplication.

Validation at documentation handoff:

- Focused conflict/recovery suite after the additional QA correction: **13 passed**, as reported by the implementing agent.
- New public-lifecycle regression covers direct conflict recovery and an initial persistence outage followed by retry. Both cases require one committed T2 alert, matching persisted/live/snapshot state, no T2 delivery before successful commit, stable retry identity and a later runner close against the recovered stop with score 2 retained.
- Independent QA found that initially rebasing the earlier T2 event to ₹150 could lower a live ₹196 stop established by a later queued candle. The correction now raises live protection only. An additional public-lifecycle regression keeps the immutable T2 event at ₹150, drains the later raise to ₹196, and verifies that a subsequent completed ₹180 close exits the remaining 20% against ₹196 while retaining score 2.
- A queued silent-watermark regression verifies that T2 recovery still advances the later completed-bar watermark without an empty alert snapshot or duplicate T2 alert; the stronger ₹150 stop, score 2 and runner state remain consistent.
- Clean final full Python suite: **1,884 passed, 45 subtests passed**, 108.69 seconds, Python 3.14.6. Twelve existing dependency/deprecation warnings; no failures or skips.
- Changed production coverage: **all 11 added executable lines and all 6 added branch arcs covered** in the isolated final report (`/tmp/ares-pr147-final-recovery-coverage.json`). This is coverage of the new recovery branch, not a claim of repository-wide 100% coverage; the broad repository target remains unmet.
- Independent final review reported no findings and confirmed the 13 focused tests passed. Final QA confirmed the frozen-source test result and full added-line/branch coverage; its approval is limited to this correction. Pushed-head CI remains pending; PR delivery metadata must identify the exact pushed commit and CI run. The earlier validation section remains the historical result for the prior follow-up.

Scope remains limited to the existing conflict-recovery contract. Expiry handling, activation/parity sign-off, production changes, merge and broker actions remain outside this correction.
