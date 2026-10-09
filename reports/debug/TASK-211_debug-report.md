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

Public unit and isolated SQL interleaving tests cover these cases. Existing watch/confirmed-signal regressions pass. No unresolved local implementation defect identified after fixes and root review. Forecast accuracy is unvalidated; displayed probabilities remain experimental.

Deployment prerequisites remain: apply the reviewed new migration with backend-only service-role access, then deploy the reviewed feature. Live transport/API behavior has not been probed with user alerts.
