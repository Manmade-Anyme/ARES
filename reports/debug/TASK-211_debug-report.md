# Debug report — TASK-211

Date: 2026-10-09. Verdict: CLEAN in local validation.

Independent read-only review identified and implementation corrected:

- Context preparation errors must preserve the original watch with an unavailable field.
- Fresh partial samples cannot make stale completed history usable, and aged partial samples cannot become finalized bars. The existing Dhan response now supplies explicit provider sample provenance.
- First-ready interaction/excursion must come from the filter's actual state and stay frozen through later movement/retries.
- Durable cancellation timestamps must not precede watch availability.
- Persisted CONSUME and bounded transport settlement precede normal confirmed-signal publication. A late known acceptance after a degraded fence is closed through the existing cancellation format.
- Duplicate/ambiguous delivery must not authorize another POST; same-token known acceptance can reconcile UNKNOWN without another request.
- New feature settings must come from config files rather than environment overrides.
- Watch workers must complete prior-run recovery before polling, using the same ephemeral launcher UUID as the application's outbox. Otherwise a worker started first can infer or post a stale prior-run watch before the application cancels it.
- Missing or invalid shared identity must preserve standalone application's direct watch delivery rather than queueing watches that the uncoordinated consumer is not authorized to deliver.
- Restart cleanup must fence later enqueue/claims from superseded producers; one cleanup UPDATE cannot prevent an old writer's delayed insert afterward. Delayed older recovery must not regain ownership, even on its first call.

Public unit and isolated SQL interleaving tests cover these cases. Existing watch/confirmed-signal regressions pass. No unresolved local implementation defect identified after fixes and root review. Forecast accuracy is unvalidated; displayed probabilities remain experimental.

The startup regression reproduced before correction (7 failed, 64 passed), with a prior-run watch reaching dispatch. The second review regression reproduced a late old-producer enqueue returning an ACTIVE row after cleanup. The corrections change the launcher, outbox, watch consumer and pending watch migration; no tuning configuration is added. The launcher freezes UUID/UTC start-time metadata, and PostgreSQL orders recovery against enqueue and base claims through a singleton lock. Delayed older recovery is rejected, active-run checks gate insertion/polling/claims, and prior-run cancellation and archival remain available. The previous unfenced recovery signature is removed. Independent final review passed across spec alignment, runtime behavior, caller contracts, scope and data flow. Real launcher tests verify both fresh values reach both children and generator failure starts neither. Unit/SQL tests verify recovery-before-poll, retry after failed recovery, current-run preservation, standalone fallback and ACTIVE-looking retired rows failing authorization. The timing-sensitive backoff assertion now permits both independent workers to encounter the same failure while retaining explicit backoff and shutdown checks.

Final local validation: 1249 tests and 45 subtests passed; worker suite 393 tests and 35 subtests passed with 100% line/branch coverage (891 statements, 266 branches); isolated single-connection PostgreSQL lifecycle 77 checks passed. Local runtime remains Python 3.14; changed production modules and the native harness parse as Python 3.10, workflow YAML, shell syntax and diff whitespace pass. The full run retains 12 existing dependency warnings. No live database writes, Discord test messages or deployment occurred.

CI adds a disposable native PostgreSQL 17.6 service and a separate-session harness for overlapping enqueue/takeover and waiting inference/base-delivery claims. Independent review verified lock order and harness/workflow construction; native execution remains pending CI because local Docker/PostgreSQL is unavailable. The PGlite lifecycle checks must not be reported as proof of concurrent lock execution.

Deployment prerequisites remain: apply the reviewed new migration with backend-only service-role access, then deploy the reviewed feature. Live transport/API behavior has not been probed with user alerts.
