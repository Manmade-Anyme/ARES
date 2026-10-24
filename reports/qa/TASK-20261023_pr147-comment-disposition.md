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

## Follow-up on head `6a2d69b`: canonical training metadata and cooldown events

| Review comment | Relationship to agreed needs | Local correction |
|---|---|---|
| [4366412916: offline CLI discards canonical option outcomes](https://github.com/Manmade-Anyme/ARES/pull/147#discussion_r4366412916) | Terminal trades must train from the retained highest milestone, while a score-1/2 OPEN runner must never become a final label. | Carry the already-fetched canonical metadata into each matching snapshot row through `row.update(metadata)`, instead of retaining only the three timestamp-related fields. Existing version-aware dataset classification receives `option_trailing`, `result_state` and nullable `trade_pnl` sourced from `trade_analytics.pnl_points`; the pipeline later exposes `pnl_points`. Legacy labeling stays unchanged. |
| [4366412920: completed-candle option exit loses cooldown reset](https://github.com/Manmade-Anyme/ARES/pull/147#discussion_r4366412920) | The new premium-monitoring path must preserve existing re-entry behavior when the original index SL closes a reference. | Pass the signal engine through every production completed-monitor caller and consume returned lifecycle events. Clear cooldown only for the existing initial `SL_HIT` event; target, break-even, ATR and option-floor events do not gain a reset trigger. |

Validation at documentation handoff:

- Runtime-adjacent suite: **78 passed**, as reported by the implementing agent. Ten new cases cover four normal monitoring paths using the real PositionManager and AresEngine, plus target/BE/ATR/floor/empty-event negatives. The four paths are normal candle fetch, final candle fetch, tick wait and fallback polling. A separate existing provider-failure test checks independent protection; it does not establish cooldown-reset behavior under that failure.
- ML-focused suite: **123 passed**, as reported by the implementing agent. The new regression runs the actual training CLI through canonical metadata fetching and the real trade-outcome preparation pipeline. It covers terminal milestone 0/1/2 trades with missing snapshot outcomes, null P&L, an OPEN runner with stale T2 telemetry, and unchanged legacy outcomes.
- Clean final full Python suite: **1,895 passed, 45 subtests passed**, 99.59 seconds, Python 3.14.6. Twelve existing dependency/deprecation warnings; no failures or skips.
- Changed production coverage: **all 6 added executable lines and both added branch arcs covered** in the final isolated report (`/tmp/ares-pr147-two-comments-coverage.json`). The repository-wide 100% target remains unmet; this result applies to the added production behavior.
- Independent final review reported no findings across 19 tests. QA passed all 11 added test cases and confirmed the frozen-source full-suite result and added-line/branch coverage. Hosted CI remains pending and must be linked to the exact pushed head in PR delivery metadata. These local results do not claim a trained/promoted model or a production release. Earlier sections remain historical validation for their respective follow-ups.

These corrections preserve the agreed index Entry/SL/T1/T2, Greeks calculations, suggested lots, milestone allocations and exit criteria. Expiry policy, chart-parity approval, activation, production migration, deployment, merge and broker actions remain outside scope.

Delivery for the preceding two-comment follow-up: commit `a2c39e0`, [CI run 40075582022](https://github.com/Manmade-Anyme/ARES/actions/runs/40075582022) passed; comments 4366412916 and 4366412920 were resolved. This is historical verification of that pushed head.

## Follow-up on head `a2c39e0`: premium-gap ordering and pending guard recovery

| Review comment | Relationship to agreed needs | Local correction |
|---|---|---|
| [4366443429: availability publishes history beyond a durable gap](https://github.com/Manmade-Anyme/ARES/pull/147#discussion_r4366443429) | After T1, an earlier option stop close-through must be processed before a later index T2 touch; a truncated history cannot prematurely increase the recorded score or booked allocation. | Move the existing durable-watermark continuity check into common option-history preparation before its result can be cached, published as available, or admitted to the replay timeline. Preserve the existing adjacent/overnight continuity policy. |
| [4366443434: stronger-stop conflict discards pending guard evidence](https://github.com/Manmade-Anyme/ARES/pull/147#discussion_r4366443434) | The post-T1 index-entry guard and its pending source evidence must survive an option-data outage and revision repair until earlier premium events can be reconciled. | Retain same-phase pending observations and unresolved guard evidence while rebasing onto the stronger committed stop. Merge persisted/pending sources by event time into every kept POST_T1 payload, including later stop raises and silent watermarks, and preserve the earliest guard marker. Keep the live same-phase journal without restoring consumed evidence into CLOSED/T2 contexts. This retains evidence rather than adding an exit trigger. |

Validation at documentation handoff:

- Six new public continuity cases cover combined market replay and premium-then-index polling, each with a missing earlier close of ₹120 or ₹200 against a ₹127 stop, plus adjacent and overnight controls. The four missing-bar cases failed before moving the check and passed afterward, as reported by the implementing agent. The ₹120 recovery must close 50% at score 1 before T2; the ₹200 recovery allows T2 only after backfill.
- Source-validation, committed-write, runtime and incident-replay suites: **54 passed**, as reported by the implementing agent.
- Four pending-guard regressions reproduced the loss before the correction. They cover immediate conflict or an initial database outage, concurrent guard sources, later live observations, durable-context restart and backfill choosing an earlier premium stop or the first index guard; the final score remains 1.
- Independent QA caught a later queued stop-raise/watermark snapshot erasing the earlier committed guard after the first rebase. The common POST_T1 merge now preserves both writers' journal in every retained snapshot. A public partial-backfill/raise/retry/restart regression verifies the durable 11:01 guard survives a local 11:02 touch and a later ₹196 stop raise, then closes at 11:01 after backfill. Another case verifies an earlier observed premium closure remains CLOSED without resurrecting pending guards.
- Final focused continuity/conflict/lifecycle/runtime/incident suite: **84 passed**. Clean final full suite: **1,907 passed, 45 subtests passed**, 75.87 seconds on Python 3.14.6; twelve existing dependency/deprecation warnings, no failures or skips.
- Final isolated coverage (`/tmp/ares-pr147-ordering-final-coverage.json`) covers **all 31 added/moved executable lines and all 24 branch arcs from added source lines** in the production diff. The repository-wide 100% target remains unmet. Compilation and diff checks passed.
- Independent frozen-source QA and four-axis review cleared the scoped correction with no remaining actionable finding. Exact-head hosted CI remains pending and must be recorded in PR delivery metadata. Earlier results above apply to their recorded heads and do not establish validation for this follow-up.

This follow-up restores existing continuity, durable-evidence and chronology guarantees. Initial index and option bracket prices, Greeks pricing, suggested lots, allocation fractions, exit criteria, expiry handling and activation rules remain unchanged. No production actions are included.
