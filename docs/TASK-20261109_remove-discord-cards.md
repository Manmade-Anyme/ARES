# TASK-20261109: Remove Discord Lifecycle Cards & Restore Standard Trade Update Format

**Date:** 2026-11-09
**Status:** implementation in progress
**Assigned to:** PM / Architect / Code Generator / QA / PR Reviewer
**Supersedes:** Card presentation section of `directives/adr/TASK-20261023_option-atr-trailing-lifecycle.md`

## Goal
Remove all "Card A" through "Card L" branding, headers, and verbose internal telemetry dumps from Discord alerts and outbox rendering. Restore the clean, standard, human-readable trade update embed format for all trade state changes (including Option ATR trailing events), matching the production format used across live Discord channels and the project frontend.

## Preserved Invariants (Do Not Change)
- All underlying Option ATR calculations, Wilder ATR smoothing, HHV period, contract pinning, and ratchet rules.
- State machines in `position_manager.py` (`OPEN`, `T1_HIT`, `T2_REACHED`, `CLOSED`, `STOPPED_OUT`).
- 3-tier milestone accounting (T1 50% / score 1, T2 30% / score 2, runner 20% / retained score 2).
- Transactional persistence in Supabase and the durable outbox deduplication mechanism.
- Outbox status acknowledgements (`DELIVERED`, `FAILED`, `AMBIGUOUS`).

## Acceptance Criteria

1. **Initial Signal Alert (`alerts.py:send_discord`)**:
   - Completely remove the `🛡️ Card A — Reference lifecycle` field from the initial signal embed.
   - Retain clean signal fields: Spot, Trade, Confidence, Entry, SL, Target, Contract, Option Entry, Target 1, Target 2, Stop Loss, Sizing, ML Prediction, Reasons.

2. **Standard Trade Update Formatting (`alerts.py:send_trade_update` and `render_option_lifecycle_event`)**:
   - Eliminate all "Card A", "Card B", "Card C", "Card D", "Card E", "Card F", "Card G", "Card H", "Card I", "Card J", "Card K", "Card L" titles and strings.
   - Title must adhere to the standard visual format:
     - Target 1 / Target 2 Hit: `🎯 🟢 #{display_id} TRADE UPDATE: {setup_type} ({direction})`
     - Stop Loss / Break-even Exit: `💥 🛑 #{display_id} TRADE UPDATE: {setup_type} ({direction})`
     - Active Trailing / Tightening: `🚨 🐂 🟢` (Bullish) or `🚨 🐻 🔴` (Bearish) `#{display_id} TRADE UPDATE: {setup_type} ({direction})`
   - Description must contain the clean, readable fields:
     - `🕒 **Time**    : {time} IST`
     - `📍 **Spot**    : **{spot:.2f}**`
     - `⚡ **Action**  : **{action_text}**`
     - `✅ **Entry**   : **{entry_price:.2f}**`
     - `🛑 **New SL**  : **{stop_loss:.2f}**`
     - `⭐ **Status**  : **{state}**`
   - Option Information:
     - For stop tightened (`ATR_SL_UPDATED` / `OPTION_SL_RATCHETED`): display `\n🛡️ **Active Option SL**: ₹ {locked_sl:.2f}`.
     - For exits: display `\n💰 **Estimated Option Exit**: ₹ {exit_price} (₹ {pnl_per_lot} / lot)\nOption figures are model estimates, not confirmed fills.`
     - For T1 Hit: display `\n🛡️ **SL Status**: Break-Even Spot {entry}; entry premium reference ₹ {entry_premium}. 50% partial-booking model.`
   - Remove `RuntimeError` that previously prevented versioned option ATR trades from using standard rendering.

3. **Silent Monitoring Events**:
   - `MONITORING_UNAVAILABLE` and `MONITORING_RESTORED` events must NOT post noisy embeds to Discord.
   - When delivered via `send_option_lifecycle_event()`, monitoring events must be safely swallowed and acknowledged as `DELIVERED` without dispatching an HTTP POST to the Discord webhook.

4. **Durable Outbox Contract Preservation**:
   - `render_option_lifecycle_event(event)` must produce payloads adhering to this clean standard embed structure so `_deliver_option_outbox` in `position_manager.py` continues to deliver reliably.
   - Retain idempotency and immutability of event objects.

5. **Test Suite Alignment**:
   - Refactor `tests/unit/test_option_lifecycle_alert_cards.py` to test the clean trade update embed schema (removing assertions for "Card [A-L]").
   - Verify `tests/unit/test_alerts.py` and all alert tests pass cleanly with 100% adherence.

## 6. Review Hardening & Edge Cases Addressed
- **Preserve None spot:** Formatted missing/None `index_price` safely using `_number(spot)` without throwing `TypeError`.
- **Source-neutral v2 exit phrasing:** Quote-triggered exits in v2 use source-neutral descriptions instead of claiming completed 5-minute candle closes.
- **Active Option SL on ATR_ACTIVE:** Added `ATR_ACTIVE` to the stop-display branch so initial activation shows locked option SL without falling through to estimated trade exit.
- **Timestamp safety:** Preserved `unavailable` for unusable timestamps instead of replacing with current wall time.
- **Normalized PERSISTENCE_FAILED:** Unified `PERSISTENCE_FAILED` with `PERSISTENCE_FAILURE` to retain warning banner and amber/red status.

## 7. Verification & QA Status
- `tests/unit/test_alerts.py`: PASSED (17/17)
- `tests/unit/test_option_lifecycle_alert_cards.py`: PASSED (21/21)
- `tests/unit/test_options_alert_reporting.py`: PASSED (37/37)
- `tests/unit/test_option_premium_protection_v2.py`: PASSED (106/106)
- `tests/unit/test_option_conflict_recovery.py`: PASSED (41/41)
- All 222 tests passing. Zero Card A-L strings. Clean standard format verified.
