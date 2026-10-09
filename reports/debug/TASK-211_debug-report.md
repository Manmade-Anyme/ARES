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

Public unit and isolated SQL interleaving tests cover these cases. Existing watch/confirmed-signal regressions pass. No unresolved local implementation defect identified after fixes and root review. Forecast accuracy is unvalidated; displayed probabilities remain experimental.

The startup regression reproduced before correction (7 failed, 64 passed), with a prior-run watch reaching dispatch. The surgical correction changes only the launcher, outbox and watch consumer, reuses the existing recovery RPC, and adds no migration or tuning configuration. Independent final review passed across spec alignment, runtime behavior, caller contracts, scope and data flow. Real launcher tests verify a fresh UUID reaches both children and generator failure starts neither. Unit/SQL tests verify recovery-before-poll, retry after failed recovery, current-run preservation and standalone fallback. The timing-sensitive backoff assertion now permits both independent workers to encounter the same failure while retaining explicit backoff and shutdown checks.

Final local validation: 1243 tests and 45 subtests passed; worker suite 387 tests and 35 subtests passed with 100% line/branch coverage (888 statements, 264 branches); isolated PostgreSQL lifecycle 60 checks passed. Local runtime remains Python 3.14; changed production modules parse as Python 3.10, shell syntax and diff whitespace pass. The full run retains 12 existing dependency warnings. No live database writes, Discord test messages or deployment occurred.

Deployment prerequisites remain: apply the reviewed new migration with backend-only service-role access, then deploy the reviewed feature. Live transport/API behavior has not been probed with user alerts.
