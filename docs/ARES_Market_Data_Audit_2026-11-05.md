# ARES Market Data Audit & Capture Playbook — 2026-11-05

**Audit Date:** 2026-11-05  
**Data Sources:** Dhan API (1-minute OHLCV spot & option data) & Live Supabase (`ares_signals`, `active_trades`, `trade_analytics`, `ml_predictions`)  
**Underlying:** NIFTY 50 (`13`, `IDX_I`)  
**Contract Expiry Audited:** 2026-11-09 (Weekly)

---

## 1. Executive Summary & Verdict

Today was a classic **Trend Reversal & Massive Short-Squeeze Day** on NIFTY 50:
- **Intraday Range:** 283.60 points (Low: 24,296.30 @ 09:15 IST $\rightarrow$ High: 24,579.90 @ 13:48 IST).
- **Net Session Change:** +158.55 points (+0.65%), closing near the highs at 24,536.60.
- **ARES Performance:** 1 signal fired (`TREND_CONTINUATION BULLISH` at 10:16 IST), which was stopped out at 10:43 IST (-25.0 spot points / -14.55 option premium points).
- **The Tragedy:** The directional call was fundamentally correct, but the trade suffered a premature entry during an incomplete retracement and was stopped out at 24,367.65 right before the market reversed and exploded +212 points higher.
- **Option Explosion:** 24400 CE rallied from a post-stop low of 122.45 to a peak of **285.55 (+133%)**; 24450 CE surged from 102.00 to **251.65 (+146%)**; 24500 CE surged from 84.00 to **219.65 (+161%)**.

---

## 2. Market Day Anatomy (Phase-by-Phase)

| Phase | Time Window | Spot Price Action | Market Dynamics | Option Behavior (24400 CE) |
|---|---|---|---|---|
| **Phase 1: Opening Liquidity Sweep** | 09:15 – 09:30 | 24,378 $\rightarrow$ 24,296 $\rightarrow$ 24,341 | Opening gap flush to 24,296.30 absorbed immediately by aggressive buyers. | Open 108.00 $\rightarrow$ Low 100.10 $\rightarrow$ 119.60 |
| **Phase 2: Morning Rally (Leg 1)** | 09:30 – 10:00 | 24,341 $\rightarrow$ 24,442.20 | Breakout above 15m & 30m ORB; establishes Morning High of 24,442.20. | 119.60 $\rightarrow$ 151.00 (+31 pts) |
| **Phase 3: Deep Pullback & ARES Stopout** | 10:00 – 10:48 | 24,442 $\rightarrow$ 24,392 (Entry) $\rightarrow$ 24,345 (Flush) | Shallow pullback false resumption @ 10:16; secondary flush to 61.8% Fib (24,345) stops out ARES. | 151.00 $\rightarrow$ 146.10 (Entry) $\rightarrow$ 131.55 (SL) $\rightarrow$ 122.45 (Low) |
| **Phase 4: Coiling Base & VWAP Reclaim** | 10:48 – 12:45 | 24,345 $\rightarrow$ 24,400 $\rightarrow$ 24,435 | Higher-low formed at 24,354. VWAP reclaimed at 11:15. Consolidation between 24,390 and 24,435 above VWAP. | 122.45 $\rightarrow$ 155.00 base building |
| **Phase 5: The Monster Short-Squeeze** | 12:55 – 13:48 | **24,442 $\rightarrow$ 24,579.90** | Decisive breakout above morning high (24,442); massive call writer squeeze (PCR uncoiled from 0.65). | **156.80 $\rightarrow$ 285.55 (+128.75 pts / +82%)** |
| **Phase 6: Climax & High Consolidation** | 13:48 – 15:30 | 24,579.90 $\rightarrow$ 24,520 $\rightarrow$ 24,536.60 | Rejection wick at 24,580; healthy consolidation holding above 24,520 into close. | 285.55 $\rightarrow$ 237.00 close |

---

## 3. Deep Dive: The ARES Live Trade (Signal #618)

### Supabase Telemetry Breakdown
```json
{
  "signal_id": 618,
  "setup_type": "TREND_CONTINUATION",
  "direction": "BULLISH",
  "trigger_price": 24392.65,
  "stop_loss": 24367.65,
  "target_1": 24417.65,
  "target_2": 24472.65,
  "contract": "24400 CE",
  "entry_premium": 146.10,
  "exit_premium": 131.55,
  "exit_reason": "SL_HIT",
  "pnl_points": -25.0,
  "option_reference_pnl": -14.55
}
```

### ML Prediction Telemetry (`ml_predictions` id=81)
- **Model Version:** `v10`
- **Win Probability:** **0.3063 (30.63%)**
- **Confidence Tier:** **LOW**
- **Key Features Observed:**
  - `pcr_oi`: 0.6498 (Bearish call heavy)
  - `atm_ce_oi_change_pct`: +4.18% (Writers actively building resistance at 24400 CE)
  - `atm_pe_oi_change_pct`: -0.74% (Put writers shedding)
  - `dist_to_pdh`: -432.05 pts

### Root Cause Analysis of the Stopout:
1. **Unfinished Pullback:** The detector triggered on the 10th candle of the pullback (the maximum allowed by `continuation_pullback_max_candles: 10`). At that moment, price had merely paused at 24,392; the underlying order book still had an imbalance of call writers pushing price down to test structural support at 24,345-24,355.
2. **Fixed 25-Point SL in High ATR Regime:** In a session with an intraday range of 283 points, a fixed 25-point stop was placed directly into the normal noise band of the retracement. The stop at 24,367.65 was only ~12 points below VWAP.
3. **ML Filter Was Not Gated:** The ML model (`v10`) accurately recognized that this setup had only a 30.6% probability of success and tagged it `LOW` confidence. Because ML gating was in shadow mode rather than enforcement mode, the trade was executed despite the system's own ML warning.

---

## 4. Why ARES Missed the 12:55 Breakout (+138 pts Spot / +128 pts Option)

1. **Absence of a Session High Breakout Detector:** ARES currently possesses `FailedBreakoutDetector` (fading false breakouts), `OIWallDetector` (fading walls), and `ExhaustionDetector` (fading overextensions). ARES has **zero** momentum/breakout detectors designed to ride genuine expansion above session highs.
2. **Rigid VWAP Distance Filter in `continuation.py`:**
   ```python
   near_vwap = abs(candle.close - candle.vwap) <= settings.continuation_pullback_vwap_pts  # 10.0 pts
   ```
   Between 11:30 and 13:00, spot held firmly between 24,400 and 24,440 while VWAP was at ~24,375 - 24,383 (distance = +25 to +58 points). Because price was too strong to pull back within 10 points of VWAP, the `TrendContinuationDetector` never armed!

---

## 5. Playbook: How We Could Have Captured Today's Move

### Play 1: The Session High Breakout (ORB Expansion) — *Recommended & Highest R:R*
- **Context:** Morning high of 24,442.20 held from 09:55 to 12:50 (nearly 3 hours of base accumulation). Heavy CE writers at 24400/24450 were vulnerable to a short squeeze if 24,442 gave way.
- **Trigger:** A 1-minute close above 24,442 accompanied by above-average volume.
  - **Timestamp:** 12:58 – 13:00 IST.
  - **Spot Entry:** 24,445 – 24,450.
  - **Option Selection:** 24450 CE @ 130.55 (or 24400 CE @ 156.80).
- **Risk Management:**
  - **Stop Loss:** Below the breakout base low at 24,420 (Spot SL = 25-30 pts; Option SL = 15 pts @ 115.00).
- **Exit Strategy:**
  - **Target 1:** 24,500 round number (reached at 13:30, Option @ 160.00, +30 pts / +23%).
  - **Target 2 (Runner):** Climax exhaustion wick when spot tested 24,575-24,580 at 13:48 IST (Option @ 245.00 - 251.00).
- **Yield:** **+115.0 to +121.0 points on 24450 CE (+88% to +92% ROI)** with Risk:Reward > 1:7.

### Play 2: Higher-Low VWAP Reclaim (The Deep Value Entry)
- **Context:** At 10:44, the market completed a 61.8% Fibonacci retracement from the morning low (24,296 $\rightarrow$ 24,442 leg), printing a low of 24,344.90 and violently bouncing back above 24,365 within 4 minutes.
- **Trigger:** Clean reclaim and hold above session VWAP (24,375) between 11:15 and 11:30 IST.
  - **Spot Entry:** 24,385 – 24,395.
  - **Option Selection:** 24400 CE @ 140.00 – 145.00.
- **Risk Management:**
  - **Stop Loss:** Below the structural swing low at 24,344 (Spot SL = 45 pts; Option SL = 22 pts @ 120.00).
- **Exit Strategy:**
  - Partial exit at morning high (24,440 @ 12:50, Option @ 156.00).
  - Trail runner behind 20 EMA until 13:50 (Exit @ 260.00+).
- **Yield:** **+115.0 to +140.0 points on 24400 CE (+80% to +100% ROI)**.

---

## 6. Actionable Engineering Fixes for ARES

1. **Activate ML Gate on Signals:**
   - Enforce: If `ml_predictions.probability < 0.45` or `confidence_tier == 'LOW'`, block signal emission or scale lot size to 0. This single rule would have spared today's -14.55 pt loss.
2. **Adaptive ATR Pullback Threshold:**
   - In `config_profiles.py`, replace fixed `continuation_pullback_vwap_pts = 10.0` with a dynamic ATR-based boundary:
     $$\text{pullback\_threshold} = \max(10.0, 1.2 \times \text{ATR}_{14})$$
     On trend days where ATR is 15-20 points, this allows the continuation detector to arm even when price remains 15-25 points above VWAP.
3. **Implement `SessionRangeBreakoutDetector`:**
   - Build a dedicated momentum breakout detector that monitors the session's first 45-minute high/low and triggers on high-volume expansion outside the range once CE/PE OI starts covering.

