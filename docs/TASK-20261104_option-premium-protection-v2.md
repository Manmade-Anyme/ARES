# Option-premium stop protection v2

## Scope

`option_premium_protection_v2` manages the premium of the exact pinned option contract (security ID, NSE_FNO, expiry, strike, and CE/PE). It does not replace or reinterpret the separate spot/index initial stop or post-T1 index break-even guard. The 5-minute option ATR formula and `option_atr_v1` parity gate remain unchanged; ATR stays disabled unless its existing config, approved-version, service-role, and backend checks pass.

## Stop lifecycle

| Phase | Effective premium stop | Exit rule |
|---|---|---|
| Before T1 | Original option-premium SL | Fresh pinned premium at or below the stop closes with `SL_HIT`, `stop_domain=OPTION_PREMIUM`. ATR has no authority. |
| At T1 | Raise to at least the sourced option entry basis | T1 and the new premium stop are written in the same lifecycle transition. The spot/index guard separately moves to spot entry. |
| After T1 | `max(previous stop, entry premium, valid ATR stop candidate)` | Fresh quote equality or a gap through the active stop closes the reference lifecycle. A candidate only takes control when strictly above the existing stop. |

Any hit of the moved entry-cost stop is `STOPPED_OUT_AT_BE` and reports `0.00` runner stop-out points, including a below-cost gap. The observed reference exit remains recorded separately, so the status and zero stop-out points do not claim a fill at cost or replace observed-price accounting. An ATR-raised stop closes with `ATR_SL_HIT`. The T1 booking and score remain. T2 remains reference-only and does not change the premium stop.

`original_option_entry` records the confirmed weighted paper-fill average only when quantity and protection confirmation are present; otherwise it records `options_sizing.entry_limit` with `entry_premium_basis_source=REFERENCE_ENTRY_LIMIT`. A trigger or reference mark is not a broker fill. No spot price is written from a premium quote.

## Data and event ordering

The monitor requests one batched Dhan quote snapshot per distinct active v2 security ID through the non-deferrable protective request lane, validates the full frozen contract pin, rejects stale/future/out-of-order data, and passes accepted timestamped observations through `PositionManager.update_option_market_events`. Lower-priority reference-accounting requests remain deferred while protection is active. The snapshot is polling data, not a tick subscription; effective cadence follows the main monitor loop. A missing or invalid quote sets monitoring to unavailable while retaining the persisted stop. A delayed quote older than the current `stop_effective_at` is rejected and marks the quote monitor unavailable until a fresh observation arrives; the current stop is never applied retroactively. Spot/index events, premium quotes, and completed option bars retain their source timestamps; a valid quote at the exact T1 timestamp is kept as order-unknown rather than assumed to be pre- or post-T1.

Paper execution routing never sends CE/PE orders to the index-candle ATR handler, including when backend gates prevent v2 enrollment; an option stop is never replaced by spot ATR. When a v2 premium trigger closes the lifecycle, the matching full-contract execution record receives an exit intent. That trigger does not become an execution fill: the order remains `EXIT_PENDING` until a later executable premium bid is observed. The execution snapshot remains separate from the reference lifecycle closure and records the eventual paper exit status and price. Reference-accounting recovery accepts both lifecycle policy versions only when the existing `one_lot_reference_v1` accounting marker is present.

ATR candidates still come only from completed, contiguous, contract-pinned 5-minute premium candles and the existing Wilder ATR(5), HHV(10) of `high - 2.5 * ATR` calculation. A candidate is applied only from its bar completion onward. While ATR is disabled or unapproved, baseline premium protection and quote checks remain active.

## Persistence and rollout

`migrations/20261104_option_premium_protection_v2.sql` replaces the lifecycle RPC with v2 policy support, enforces immutable contract and premium basis, monotonic premium-stop/quote watermarks and the post-T1 entry-cost floor, and adds a service-role-only capability RPC. It contains a read-only open-record preview and performs no trade enrollment or historical rewrite. New trades enroll only when this backend capability is ready. Existing v1 and unversioned trades are not retrofitted.

The code change does not apply the migration, alter runtime config, enable ATR, deploy, restart a worker, or submit orders. Production v2 protection requires the migration and the separately reviewed runtime rollout. Until that occurs, records that cannot pass the v2 backend capability check remain unenrolled in premium protection and are explicitly excluded from spot ATR fallback.

## Verification boundary

Unit lifecycle tests and isolated PGlite transaction tests cover initial premium SL, T1 break-even movement, quote equality/gaps, CE/PE symmetry, ATR precedence, wrong/stale contract data, monotonic state, status labels, runner points, and migration behavior. Synthetic tests do not establish live Dhan quote latency or incident-time breach marks. The 4 November zero-lot `SIGNAL_REFERENCE` incident records establish the legacy spot-ATR route and 10:20 close, but the durable store has no intermediate option quote/candle series to establish the corrected historical trigger time or any broker fill.
