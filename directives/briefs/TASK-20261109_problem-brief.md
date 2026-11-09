# Problem Brief — TASK-20261109
**Date:** 2026-11-09
**Status:** approved

## Problem Statement (Original)
User request:
"First of all, what are these cards? I don't like these card implementations to give me status update. J card, A card, B card. I am not liking this. This is not the way I want to see the updates in Discord. I want you to remove this card implementation entirely from this project and remove all the traces of these card implementations in terms of giving me updates. Previously we were getting updates the way it's mentioned in the screenshot which I have attached above. I want to see the updates in this way. The reason is that these updates are going to be shown on the front end of this project also. So having these card implementations with so much of data is very difficult to read it in live market. The previous implementation was absolutely alright. I have been seeing this for a long time now and I am not at all liking this. I want to see the old implementation. With the new ATR trailing I want to see all the proper updates in the form as mentioned above in the screenshot."

## Problem Statement (Simplified)
Remove all "Card A" through "Card L" naming and debug telemetry dumps from Discord alerts and outbox payloads. Revert all Discord alerts and trade updates to the clean, human-readable format shown in the legacy screenshot while preserving the underlying Option ATR calculations and state management.

## What We Know
- Initial signal alert in `alerts.py:send_discord()` appends a verbose `🛡️ Card A — Reference lifecycle` field containing internal telemetry when option trailing is active.
- Lifecycle event updates in `alerts.py:render_option_lifecycle_event()` render embeds titled `#{id} · Card {card} · {action}` with extensive internal debugging dumps.
- In `alerts.py`, `send_trade_update()` and `_option_lifecycle_estimate()` raise `RuntimeError("Versioned lifecycle alerts require persisted immutable event snapshots")` when `option_atr_v1` or `option_premium_protection_v2` is present, forcing trades into the verbose Card format.
- The user requires the clean legacy format:
  - Title with status-specific emoji:
    - Target 1 / Target 2 Hit: `🎯 🟢`
    - Stop Loss Hit / Stopped Out at BE: `💥 🛑`
    - Standard Directional Updates: `🚨 🐂 🟢` / `🚨 🐻 🔴`
  - Body:
    - Time, Spot, Action, Entry, New SL, Status
    - Clean option estimates / ATR line: `💰 Estimated Option Exit: ₹ ... (₹ ... / lot)` or `🛡️ Active Option SL: ₹ [Option Premium SL]`
  - Monitoring delay/restored events (`MONITORING_UNAVAILABLE`, `MONITORING_RESTORED`): silenced completely on Discord.

## What We Don't Know (Assumptions Validated)
- *Clarification 1*: Option ATR Stop Tightened update format -> confirmed: include `🛡️ Active Option SL : ₹ [Option Premium SL]`.
- *Clarification 2*: Monitoring delay/restored alerts -> confirmed: silenced completely on Discord to prevent live market distraction.
- *Clarification 3*: Icons -> confirmed: `🎯 🟢` for Target 1/2 hit, `💥 🛑` for SL / BE exit, `🚨 🐂 🟢` / `🚨 🐻 🔴` for active directional updates.

## Edge Cases Identified
- **Durable Outbox Delivery:** `position_manager.py:_deliver_option_outbox` relies on `send_option_lifecycle_event()`. That function must still deliver cleanly formatted payloads and return `DELIVERED`, `FAILED`, or `AMBIGUOUS` without mutability or transport failures.
- **Silenced Monitoring Events:** When `send_option_lifecycle_event()` receives `MONITORING_UNAVAILABLE` or `MONITORING_RESTORED`, it must acknowledge delivery in the outbox as `DELIVERED` (or return early with successful acknowledgement) without dispatching an HTTP POST to Discord.
- **Persistence Failure Warning:** Operational warning (`PERSISTENCE_FAILURE`) should either be logged/silenced or sent as a clean system alert without "Card L" branding.
- **Option Exit Estimates:** On exit, `_option_lifecycle_estimate` must display estimated exit premium and PnL per lot without raising `RuntimeError`.

## Recommended Next Step
Proceed to Directive creation, write failing tests first (TDD), implement changes in `alerts.py`, update unit tests, and verify 100% test pass.
