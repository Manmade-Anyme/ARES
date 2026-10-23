# ADR-20261023: Pinned Option-Premium ATR Trailing, Milestone Accounting, Transactional Persistence, and Discord Alerts

- **Status**: Proposed / In Review
- **Date**: 2026-10-23
- **Task ID**: TASK-20261023
- **Author**: Software Architect Agent
- **Target Branch**: `feature/TASK-20261023-option-atr`
- **Supersedes**: `directives/adr/TASK-20261023_t2-milestone-alert.md`

---

## 1. Context and Problem Statement

ARES previously implemented post-T1 trailing stops on underlying NIFTY spot index candles (`atr_trailing.py`). Under this architecture:
1. **Spot vs Premium Distortion**: Trailing an option runner using spot index ATR and projecting an "Estimated Option SL" via second-order Greeks Taylor approximation introduces non-linear tracking errors. Greeks decay, volatility shifts, and intraday spread variations make synthetic spot-projected premium stops diverge from actual option market dynamics.
2. **Missing Post-T1 Index Protection**: In `position_manager.py:441`, once a trade reached `T1_HIT`, normal spot candle evaluations were completely bypassed. Consequently:
   - Index entry stop-loss (break-even protection) was never monitored between T1 and T2.
   - Index Target 2 (T2) milestone was never recorded or notified.
3. **Premature Terminal Accounting**: `T2_HIT` was treated by startup reconciliation as a terminal state (`position_manager.py:96`), preventing the runner from continuing after T2. In `storage.py:320-334`, `trade_analytics.score` was coupled to `result_state` and could only be persisted alongside an `exit_timestamp` and `exit_price`, causing premature trade closure or stuck rows.
4. **Spot/Option ATR Multi-Consumer Divergence**: `main.py`, `position_manager.py`, and `execution/order_manager.py` each independently called `completed_atr_bars` on index candles, creating divergent stop levels and conflicting watermarks.

---

## 2. Decision and Architecture Overview

### 2.1 Preserved Core Calculations (Invariants)
The following subsystems remain 100% unchanged:
- Index signal detection, confidence gates, entry, initial SL, T1, and T2 levels.
- Greeks-based strike selection, option entry limit, original option catastrophic SL, and option T1/T2 references.
- Suggested lots, risk budget, capital validation, and entry-to-SL risk.
- **Index milestones govern the trade**: Index spot touches trigger T1 and T2. Option target levels remain non-executable reference labels.

### 2.2 Pinned Contract Lifecycle & Milestone Accounting
Every trade retains a single canonical `trade_id` across its lifecycle:

```
[Entry] -> 100% Active. Protected by Original Index SL. Option ATR inactive. Score: NULL.
   │
   ├─► [Index SL Touched] ──► Close 100%. Score: 0. Status: CLOSED. (SL_HIT)
   │
   └─► [Index T1 Reached] ──► Model-book 50%, 50% remains. Score: 1. Status: OPEN (T1_HIT).
          │                  Move Index SL to Index Entry.
          │                  Look up latest eligible completed 5m Option ATR line.
          │                  If Raw ATR > Original Option SL: Lock Active Stop = Raw ATR.
          │                  Else: Await completed bar where Raw ATR > Original Option SL.
          │
          ├─► [Index Entry Touched (BE)] ──► Close remaining 50%. Score: 1. Status: CLOSED. (STOPPED_OUT_AT_BE)
          ├─► [Option ATR Close Hit]     ──► Close remaining 50%. Score: 1. Status: CLOSED. (ATR_SL_HIT)
          │
          └─► [Index T2 Reached] ──► Model-book 30% (80% cumulative). Score: 2. Status: OPEN (T2_REACHED).
                 │                   20% runner remains. Disable Index SL protection completely.
                 │                   If ATR has not exceeded Original Option SL: Fallback Floor = Original Option SL.
                 │
                 └─► [Option Stop Close Hit] ──► Close final 20%. Score: 2 retained. Status: CLOSED.
                                                (ATR_SL_HIT or OPTION_SL_FLOOR_HIT)
```

- **Touch vs Close**: Index stop and target levels use inclusive touch detection (tick or wick). Option stop exits require an explicitly **completed 5-minute option candle closing strictly below the active stop** (`close < active_stop`). Intra-bar wicks and equal closes do not trigger an option exit.
- **Upward-Only Ratchet**: Both CE and PE option long premium stops ratchet upwards only:
  $$\text{locked\_stop} = \max(\text{original\_option\_sl}, \text{previous\_locked\_stop}, \text{new\_confirmed\_raw\_line})$$

---

## 3. Pure Indicator & Parity Architecture

### 3.1 Pinned Contract Ingestion (`options_candles.py`)
- Contract attributes frozen at entry: `security_id`, `exchange_segment`, `expiry`, `strike`, `option_type`.
- Native 5-minute candles are fetched for the pinned `security_id`.
- Timestamp normalization: source timestamp denotes bar start in IST; bar end is derived explicitly (`start + 5 minutes`).
- A bar is eligible for evaluation only after `now >= bar_end`. Forming, partial, out-of-order, or malformed bars are rejected.

### 3.2 Indicator Specification & Candidate Investigation
- **Settings**: Timeframe: 5m, ATR Period: 5 (Wilder smoothing), HHV Lookback: 10, Multiplier: 2.5.
- Continuous multi-session calculation: True Range and Wilder smoothing carry over across session boundaries; overnight boundaries do not reset the indicator.
- **Formula 1 (Candidate)**:
  $$\text{raw\_line}_i = \max_{k=0..9} \left( \text{High}_{i-k} - 2.5 \times \text{ATR5}_{i-k} \right)$$
- **Formula 2 (Legacy Alternative)**:
  $$\text{raw\_line}_i = \left( \max_{k=0..9} \text{High}_{i-k} \right) - 2.5 \times \text{ATR5}_i$$
- **Parity Gate**: Formula 1 aligns with TradingView's classic `ATR TS` / Ceyhun Pine implementation. Multi-session warmup history explains fractional differences (e.g., ₹0.57) observed when indicators are seeded intraday vs continuously. Live production activation remains gated until parity against Dia's chart feed is formally verified.

---

## 4. Canonical State & Database Persistence

### 4.1 Versioned Context Schema (`trade_analytics.market_context.option_trailing`)
```json
{
  "policy_version": "option_atr_v1",
  "revision": 3,
  "contract": {
    "security_id": "51333",
    "exchange_segment": "NSE_FNO",
    "expiry": "2026-10-27",
    "strike": 22550,
    "option_type": "PE"
  },
  "phase": "POST_T2",
  "highest_milestone": 2,
  "t1_at": "2026-10-23T10:09:00+05:30",
  "t2_at": "2026-10-23T10:15:00+05:30",
  "booked_fraction": 0.80,
  "remaining_fraction": 0.20,
  "original_option_sl": 123.65,
  "index_guard_enabled": false,
  "index_guard_stop": null,
  "atr_period": 5,
  "hhv_period": 10,
  "multiplier": 2.5,
  "atr_eligible": true,
  "protection_mode": "ATR",
  "raw_line": 148.15,
  "locked_option_sl": 148.15,
  "previous_locked_option_sl": 131.63,
  "stop_effective_at": "2026-10-23T11:00:00+05:30",
  "last_processed_option_bar_end": "2026-10-23T11:55:00+05:30",
  "indicator_source_version": "tv_atr_ts_v1",
  "monitoring_status": "ACTIVE",
  "last_valid_option_bar_end": "2026-10-23T11:55:00+05:30",
  "exit": null,
  "alert_events": {}
}
```

### 4.2 State and Scoring Separation
- `active_trades.state`: `OPEN` (Pre-T1) $\rightarrow$ `T1_HIT` (Post-T1, 50% remaining) $\rightarrow$ `T2_REACHED` (Post-T2, 20% runner remaining) $\rightarrow$ `CLOSED`.
- `trade_analytics.result_state`: Remains `OPEN` while any modeled allocation remains. Only set to terminal exit reason (`SL_HIT`, `STOPPED_OUT_AT_BE`, `ATR_SL_HIT`, `OPTION_SL_FLOOR_HIT`) when remaining allocation reaches 0%.
- `trade_analytics.score`: Set to `1` upon reaching T1, `2` upon reaching T2, without setting `exit_timestamp`. Terminal exit retains the highest milestone score (`trade_analytics.score`).
- `ml_collection` backfill: Only rows with `result_state != 'OPEN'` and valid `exit_timestamp` become finalized ML training examples.

### 4.3 Transactional Persistence Contract (`apply_option_lifecycle_transition`)
Postgres RPC or atomic SQL transaction ensuring:
1. Row lock via `SELECT FOR UPDATE` on `active_trades` and `trade_analytics`.
2. Revision checking: rejects stale updates where `current_revision != expected_revision`.
3. Event deduplication: `event_id` idempotency prevents duplicate allocation booking.
4. Deep JSON merge of `market_context.option_trailing` preserving sizing, Greeks, and execution telemetry.
5. Atomic update of `active_trades` and `trade_analytics`.

---

## 5. Discord Message Contract & Delivery Rules

Messages are constructed from immutable event snapshots:
- **Card A (New Signal)**: Clear reference tracking disclaimer, original index SL, Greeks option levels, and notice that Option ATR starts after Index T1.
- **Card B (T1 Reached)**: Index T1 level, 50% model-booked, recorded score 1, index SL moved to entry, initial ATR status.
- **Card C (ATR Active)**: Emitted when raw ATR later exceeds original option SL floor.
- **Card D (Option SL Raised)**: Emitted only when locked SL increases.
- **Card E (T2 Reached)**: Index T2 level, additional 30% model-booked (80% total), 20% runner remaining, recorded score 2, index protection disabled.
- **Card F (Original Index SL Exit)**: 100% closed, score 0.
- **Card G (Index Entry Exit after T1)**: Remaining 50% closed, score 1 retained, explicit note: "Index break-even does not guarantee option break-even."
- **Card H (ATR Exit before T2)**: Remaining 50% closed, score 1 retained.
- **Card I (Runner Exit after T2)**: Final 20% runner closed, score 2 retained.
- **Card J/K (Monitoring Unavailable / Restored)**: Discloses stale data (> 60s past bar end during market hours).
- **Card L (Persistence Failure)**: Operational alert distinguishing in-memory observed state from unconfirmed database state.

---

## 6. Deprecation & Isolation of Legacy Paths

- Spot-based `completed_atr_bars` calls in `PositionManager` and `main.py` are superseded for trades using `option_atr_v1`.
- `execution/order_manager.py` and `execution/signal_dispatch.py` are isolated: paper execution tracking does not overwrite canonical `option_trailing` telemetry.
- Projected "Estimated Option SL" using spot Greeks is removed from Discord trailing alerts.

---

## 7. Definition of Done & Verification Plan

1. **Indicator Parity Evidence**: Detailed report comparing Formula 1 vs Formula 2 with frozen fixtures.
2. **Deterministic Replay**: Complete chronological replay reports for #1950 (multi-milestone lifecycle) and #3935 (pre-T1 SL exit).
3. **Automated Test Matrix**: 100% line and branch coverage across all new modules and modified methods.
4. **Migration & Rollout Safety**: Idempotent SQL migration script and migration preview query for any open database records.
