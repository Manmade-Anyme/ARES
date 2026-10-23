# TASK-20261023: Option-Premium ATR Trailing, Milestone Accounting, and Discord Alerts

**Date:** 2026-10-23
**Status:** in-progress
**Assigned to:** PM / Architect / Code Generator / QA / PR Reviewer
**Supersedes:** `TASK-20261023-t2-milestone` (cancelled T2-only alert implementation)

## Goal

Replace post-T1 spot-based ATR trailing with option-premium ATR trailing on completed 5-minute candles for pinned option contracts, implement 3-tier milestone accounting (T1 50% / score 1, T2 30% / score 2, runner 20% / retained score 2), enforce strict event ordering and dual protection (index break-even + option ATR between T1 and T2, option-only after T2), establish transactional persistence with score and state separation in Supabase, and deliver an immutable Discord alert message catalog.

## Preserved Invariants (Do Not Change)

- Index entry, original index SL, T1, and T2 spot levels.
- Greeks-based option entry, original option SL, option T1 reference, and option T2 reference.
- Suggested lots, risk budget, capital checks, and option entry-to-SL risk calculations.
- Signal detection, confidence gates, strike selection, and initial entry eligibility.
- Index levels trigger T1 and T2 milestones; option target estimates remain displayed references.

## Acceptance Criteria

1. **Indicator Parity & Contract Pinning**:
   - Pin contract at entry: `security_id`, `exchange_segment`, `expiry`, `strike`, `option_type`, original option entry, and original option SL. Never switch to a newer ATM contract as spot moves.
   - Load option OHLC history for the exact pinned contract, rejecting forming, malformed, duplicate, or wrong-contract bars.
   - Pure ATR indicator: 5-minute timeframe, ATR period 5 (Wilder smoothing), HHV period 10, multiplier 2.5. Computed continuously across sessions without artificial overnight resets.
   - Investigate and document parity between candidate formula $HHV_{10}(High_i - 2.5 \times ATR5_i)$ and Dia's chart indicator, delivering an explicit discrepancy report and freezing test fixtures. Gated from live production activation if discrepancy remains unverified.

2. **Milestone Lifecycle & Dual Protection**:
   - **Before T1**: 100% allocation protected by original index SL. Option ATR inactive. Recorded score: NULL. Lifecycle: OPEN.
   - **Index SL Hit Before T1**: Close 100%. Recorded score: 0. Lifecycle: CLOSED.
   - **Index T1 Reached**: Model-book 50%, 50% remains. Move index SL to index entry. Enable option ATR management. Recorded score: 1. Lifecycle: OPEN.
   - **Between T1 and T2**: Index entry SL (touch) and eligible option ATR stop (completed 5m close strictly below) operate concurrently. First valid exit closes the remaining 50% (score 1, CLOSED).
   - **Index T2 Reached**: Model-book another 30% (80% cumulative, 20% runner remains). Disable index SL protection. Recorded score: 2. Lifecycle: OPEN.
   - **After T2**: Option protection alone governs the 20% runner. If ATR has not exceeded original option SL by T2, original option SL serves as fallback floor. Completed 5m candle closing strictly below active stop closes final 20% (score 2 retained, CLOSED).
   - Single trade ID covers full lifecycle (no separate runner trade).

3. **Event Ordering & Chronology**:
   - Process events in strict chronological order using source event time, separate from detection time.
   - For identical timestamps, evaluate index milestones before option candle closes; option exit uses stop valid for that candle.
   - Preserve target-first resolution when a single index bar touches both target and stop.
   - If one index observation establishes T1 and T2, apply T1 then T2 exactly once.
   - Never use pre-activation candle wicks or closes to trigger post-activation stops.

4. **Persistence & Scoring Separation**:
   - Add canonical versioned state under `trade_analytics.market_context.option_trailing`.
   - Active phases represented as `OPEN`, `T1_HIT`, and `T2_REACHED` in `active_trades`. Only terminal exits mark trade closed.
   - Keep `trade_analytics.result_state = 'OPEN'` while any modeled allocation remains.
   - Update `score` to 1 at T1 and 2 at T2 without setting `exit_timestamp`.
   - On terminal closure, persist actual terminal exit reason (`SL_HIT`, `STOPPED_OUT_AT_BE`, `ATR_SL_HIT`, `OPTION_SL_FLOOR_HIT`) and retain highest milestone score.
   - Stored procedure / transactional transition `apply_option_lifecycle_transition` enforcing atomic updates, revision locking, and idempotency.
   - Option premiums stored separately; never injected into index price fields or index P&L calculations.

5. **Discord Alert Message Catalog**:
   - Immutable event snapshots rendering exact catalog cards (A through L).
   - Remove projected "Estimated Option SL"; display exact locked option stop.
   - Display clear reference tracking disclaimers (modeled allocation, no broker fill claims).
   - Idempotent alert dispatch suppressing duplicate transitions, unchanged stops, and stale alerts after closure.

6. **Verification & Incidents**:
   - 100% test coverage of all new and modified components.
   - Offline replay of #1950 (demonstrating T1, T2, ATR line, locked stops, allocations, scores, and rendered Discord messages).
   - Offline replay of #3935 (demonstrating pre-T1 index SL exit unchanged).
   - Delivery package: PR URL, base/head SHA, migration preview, parity report, and replay evidence. No direct merge or deployment.
