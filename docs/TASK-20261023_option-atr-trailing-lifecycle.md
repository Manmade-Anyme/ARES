# Option-Premium ATR Trailing, Milestone Accounting, and Discord Alerts

**Task:** TASK-20261023  
**Status:** Implemented  
**Date:** 2026-10-23  
**Branch:** `feature/TASK-20261023-option-atr`  

---

## 1. Executive Summary

This architecture supersedes post-T1 spot-based ATR trailing with pinned option-premium ATR trailing on completed 5-minute candles. Initial entry calculations, Greeks limit pricing, risk budgeting, and spot levels (Index Entry, SL, T1, T2) remain unchanged. Index levels trigger T1 and T2 milestones; option stops govern the runner.

### Lifecycle & Milestone Accounting Matrix

| Event | Allocation & Protection | Recorded Score | Lifecycle Result State | Active Trades State |
|---|---|---:|---|---|
| **Before T1** | Original Index SL protects 100%. Option ATR inactive. | `NULL` | `OPEN` | `OPEN` |
| **Index SL Hit Before T1** | Close 100%. | `0` | `SL_HIT` | `CLOSED` |
| **Index T1 Reached** | Model-book 50%; 50% remains. Index SL moved to entry break-even. Option ATR management enabled. | `1` | `OPEN` | `T1_HIT` |
| **Index Entry Touched (T1-T2)** | Close remaining 50%. (Index break-even does not guarantee option break-even). | `1` | `STOPPED_OUT_AT_BE` | `CLOSED` |
| **Option ATR Exit (T1-T2)** | Completed 5m close strictly below active stop closes remaining 50%. | `1` | `ATR_SL_HIT` | `CLOSED` |
| **Index T2 Reached** | Model-book additional 30% (80% cumulative booked, 20% runner remains). Index SL protection disabled. Option fallback floor active at original option SL if ATR line not yet qualified. | `2` | `OPEN` | `T2_REACHED` |
| **Option Stop Exit After T2** | Completed 5m close strictly below active stop closes final 20%. | `2` (retained) | `ATR_SL_HIT` / `OPTION_SL_FLOOR_HIT` | `CLOSED` |

---

## 2. Mathematical Specification & Parity

### ATR TS Specification (5-minute timeframe)
- **ATR Period:** 5 (Wilder smoothing: $\text{ATR}_i = \frac{\text{ATR}_{i-1} \times 4 + \text{TR}_i}{5}$)
- **HHV Period:** 10
- **Multiplier:** 2.5
- **Candidate Indicator Formula:**
  $$\text{raw\_line}_i = \max_{k=0..9} \left( \text{High}_{i-k} - 2.5 \times \text{ATR5}_{i-k} \right)$$
- **Ratchet Rule:**
  $$\text{locked\_stop}_i = \max(\text{locked\_stop}_{i-1}, \text{raw\_line}_i)$$
  *(Only eligible when $\text{raw\_line} > \text{original\_option\_sl}$)*

---

## 3. Database State & RPC Procedure

### Canonical State (`trade_analytics.market_context.option_trailing`)
Versioned under `policy_version: "option_atr_v1"`, tracking:
- `contract`: Pinned `security_id`, `exchange_segment`, `expiry`, `strike`, `option_type`.
- `phase`: `PRE_T1` | `POST_T1` | `POST_T2` | `CLOSED`.
- `highest_milestone`: `0` | `1` | `2`.
- `booked_fraction`, `remaining_fraction`.
- `locked_option_sl`, `protection_mode`.
- `last_processed_option_bar_end`.
- `alert_events`: Idempotent delivery tracking preventing duplicate alerts.

### Transactional RPC (`apply_option_lifecycle_transition`)
Stored procedure in `migrations/20261023_option_lifecycle_transition.sql` providing:
1. `SELECT FOR UPDATE` on `active_trades` and `trade_analytics`.
2. Revision checking preventing concurrent race conditions.
3. Event deduplication on `event_id`.
4. Score and state separation: `score` updated to 1 or 2 while `result_state` remains `'OPEN'`.
5. Retaining highest milestone score upon final terminal closure.

---

## 4. Discord Message Catalog (Cards A - L)

- **Card A (New Signal):** Reference tracking model disclaimer, original index SL, Greeks option levels, notice that option ATR begins post-T1.
- **Card B (T1 Reached):** Index T1 level, 50% model-booked, recorded score 1, index SL moved to entry break-even, option ATR status.
- **Card C (ATR Active):** Emitted when raw ATR strictly exceeds original option SL floor.
- **Card D (Option SL Raised):** Emitted only when effective locked stop increases.
- **Card E (T2 Reached):** Index T2 level, additional 30% model-booked (80% cumulative), score 2 recorded, index SL disabled, runner 20% remaining.
- **Card F (Index SL Exit):** Pre-T1 index SL hit, 100% closed, score 0.
- **Card G (Index Entry Exit):** Post-T1 break-even touch, remaining 50% closed, score 1 retained, explicit disclaimer that spot BE != option BE.
- **Card H (ATR Exit Pre-T2):** Completed 5m candle closes below stop, remaining 50% closed, score 1 retained.
- **Card I (Runner Exit Post-T2):** Completed 5m candle closes below stop, final 20% closed, score 2 retained.
- **Card J / K (Monitoring Unavailable / Restored):** Warning when option candles delayed > 60s past candle close.
- **Card L (Persistence Failure):** Operational alert when DB persistence fails while memory state remains active.
