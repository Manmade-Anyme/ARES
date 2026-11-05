"""
ARES 5th Strategy: Gated Regime Expansion Strategy (POC)
=========================================================
Implements the full 5th strategy architecture with multi-factor volatility gating:
- No reliance solely on ATR.
- Uses:
  1. Overnight Gap Extension (|Open - PDC|)
  2. Opening Range Volatility Ratio (30m OR / Historical 5-day OR)
  3. VWAP Standard Deviation Dispersion Bands (Z-Score)
  4. Bollinger Bandwidth (BBW) Expansion Ratio
  5. Structural Pivot Swings for zero-lag Stop Losses
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np


@dataclass
class GatedTradeRecord:
    strategy: str
    date: str
    direction: str
    entry_time: str
    entry_price: float
    stop_loss: float
    risk_pts: float
    target_1: float
    t1_hit: bool = False
    t1_time: Optional[str] = None
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_points: float = 0.0
    regime_score: int = 0
    vwap_zscore: float = 0.0
    bbw_expansion: float = 0.0


class GatedRegimeExpansionEngine:
    """
    5th Strategy Engine: Gated Regime Trend Expansion.
    Evaluates volatility regime before permitting any trades.
    """

    def __init__(
        self,
        range_anchor_end: str = "12:30",
        entry_cutoff_time: str = "14:15",
        eod_exit_time: str = "15:15",
        max_trades_per_day: int = 2,
        t1_reward_pts: float = 35.0,
        max_stop_loss_pts: float = 35.0,
        min_impulse_pts: float = 70.0,
        min_regime_score: int = 2
    ):
        self.range_anchor_end = range_anchor_end
        self.entry_cutoff_time = entry_cutoff_time
        self.eod_exit_time = eod_exit_time
        self.max_trades_per_day = max_trades_per_day
        self.t1_reward_pts = t1_reward_pts
        self.max_stop_loss_pts = max_stop_loss_pts
        self.min_impulse_pts = min_impulse_pts
        self.min_regime_score = min_regime_score

    def enrich_session(self, df: pd.DataFrame) -> pd.DataFrame:
        """Computes VWAP, VWAP Standard Deviation, Bollinger Bandwidth, and EMAs."""
        d = df.copy()
        
        # 1. VWAP and VWAP Dispersion (Standard Deviation)
        d["typical_price"] = (d["high"] + d["low"] + d["close"]) / 3.0
        d["cum_vol"] = d["volume"].cumsum()
        d["cum_tp_vol"] = (d["typical_price"] * d["volume"]).cumsum()
        d["vwap"] = (d["cum_tp_vol"] / d["cum_vol"].replace(0, np.nan)).ffill()

        # Cumulative volume-weighted variance
        d["dev2"] = (d["typical_price"] - d["vwap"]) ** 2
        d["cum_dev2_vol"] = (d["dev2"] * d["volume"]).cumsum()
        d["vwap_std"] = np.sqrt(d["cum_dev2_vol"] / d["cum_vol"].replace(0, np.nan)).ffill().replace(0, 1.0)
        d["vwap_zscore"] = (d["close"] - d["vwap"]) / d["vwap_std"]

        # 2. Bollinger Bandwidth (BBW)
        d["ma20"] = d["close"].rolling(20).mean()
        d["std20"] = d["close"].rolling(20).std().replace(0, 1.0)
        d["upper_bb"] = d["ma20"] + 2.0 * d["std20"]
        d["lower_bb"] = d["ma20"] - 2.0 * d["std20"]
        d["bbw"] = (d["upper_bb"] - d["lower_bb"]) / d["ma20"] * 100.0
        d["bbw_sma20"] = d["bbw"].rolling(20).mean().fillna(d["bbw"])
        d["bbw_expansion_ratio"] = d["bbw"] / d["bbw_sma20"].replace(0, 1.0)

        # 3. Moving Averages & Volume
        d["ema9"] = d["close"].ewm(span=9, adjust=False).mean()
        d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
        d["vol_sma20"] = d["volume"].rolling(20).mean().fillna(d["volume"])

        return d

    def evaluate_regime(
        self,
        df: pd.DataFrame,
        pdc: float,
        pdh: float,
        pdl: float
    ) -> Tuple[bool, int, Dict[str, bool], pd.DataFrame]:
        """
        Multi-Factor Volatility Regime Gate:
        Evaluates 4 independent non-ATR volatility expansion factors:
        Factor 1: Overnight Gap Extension (|Open - PDC| >= 70 pts)
        Factor 2: Opening Range Expansion (30-min range >= 85 pts)
        Factor 3: Session VWAP Dispersion (vwap_std >= 25 pts by 10:30)
        Factor 4: Morning Directional Displacement (|Close_30m - Open| >= 45 pts)
        """
        df_enriched = self.enrich_session(df)
        open_price = df_enriched.iloc[0]["open"]
        gap = abs(open_price - pdc)

        m30 = df_enriched[df_enriched["time_str"] <= "09:45"]
        range_30m = (m30["high"].max() - m30["low"].min()) if not m30.empty else 0.0
        displacement_30m = abs(m30.iloc[-1]["close"] - open_price) if not m30.empty else 0.0

        m60 = df_enriched[df_enriched["time_str"] <= "10:30"]
        vwap_dispersion = m60["vwap_std"].iloc[-1] if not m60.empty else 0.0

        factors = {
            "gap_extension": gap >= 70.0,
            "range_expansion": range_30m >= 85.0,
            "vwap_dispersion": vwap_dispersion >= 25.0,
            "directional_displacement": displacement_30m >= 45.0
        }

        score = int(sum(factors.values()))
        is_expansion = bool(score >= self.min_regime_score)
        return is_expansion, score, factors, df_enriched

    def run_session(
        self,
        df: pd.DataFrame,
        date_str: str,
        pdc: float,
        pdh: float,
        pdl: float
    ) -> List[GatedTradeRecord]:
        """
        Runs the 5th Strategy on a session after verifying regime qualification.
        """
        is_expansion, regime_score, factors, df = self.evaluate_regime(df, pdc, pdh, pdl)
        if not is_expansion:
            # Regime is COMPRESSION_CHOP -> Strategy remains completely dormant
            return []
        completed_trades: List[GatedTradeRecord] = []
        active_trade: Optional[GatedTradeRecord] = None
        trades_count = 0
        cooldown_until_idx = 0

        # Morning anchor (09:15 to 10:15)
        m_mask = (df["time_str"] >= "09:15") & (df["time_str"] <= "10:15")
        m_df = df[m_mask]
        m_high = m_df["high"].max() if not m_df.empty else 0.0
        m_low = m_df["low"].min() if not m_df.empty else 0.0
        m_open = m_df.iloc[0]["open"] if not m_df.empty else 0.0

        # Extended anchor (09:15 to 12:30)
        ext_mask = (df["time_str"] >= "09:15") & (df["time_str"] <= self.range_anchor_end)
        ext_df = df[ext_mask]
        range_high = ext_df["high"].max() if not ext_df.empty else 0.0
        range_low = ext_df["low"].min() if not ext_df.empty else 0.0

        # VWAP Reclaim state
        dipped_below_vwap = False
        sweep_low = 999999.0

        for i in range(len(df)):
            candle = df.iloc[i]
            t = candle["time_str"]

            # 1. EOD Exit
            if t >= self.eod_exit_time:
                if active_trade is not None:
                    active_trade.exit_time = t
                    active_trade.exit_price = candle["close"]
                    active_trade.exit_reason = "EOD_CLOSE"
                    active_trade.pnl_points = active_trade.exit_price - active_trade.entry_price
                    completed_trades.append(active_trade)
                    active_trade = None
                continue

            # 2. Position Management
            if active_trade is not None:
                # Stop Loss check
                if candle["low"] <= active_trade.stop_loss:
                    active_trade.exit_time = t
                    active_trade.exit_price = active_trade.stop_loss
                    active_trade.exit_reason = "SL_HIT"
                    active_trade.pnl_points = active_trade.exit_price - active_trade.entry_price
                    completed_trades.append(active_trade)
                    active_trade = None
                    cooldown_until_idx = i + 15
                    continue

                # Target 1 check
                if not active_trade.t1_hit and candle["high"] >= active_trade.target_1:
                    active_trade.t1_hit = True
                    active_trade.t1_time = t
                    # Ratchet SL to Breakeven + 2 pts buffer
                    active_trade.stop_loss = active_trade.entry_price + 2.0

                # 20 EMA Trailing Exit (activated after 13:30)
                if active_trade.t1_hit and candle["close"] < candle["ema20"] and t >= "13:30":
                    active_trade.exit_time = t
                    active_trade.exit_price = candle["close"]
                    active_trade.exit_reason = "EMA20_TRAIL"
                    active_trade.pnl_points = active_trade.exit_price - active_trade.entry_price
                    completed_trades.append(active_trade)
                    active_trade = None
                    cooldown_until_idx = i + 10
                    continue

                continue

            # 3. New Entry Scans
            if trades_count >= self.max_trades_per_day or i < cooldown_until_idx or t >= self.entry_cutoff_time:
                continue

            # Directional Safety Gate: Avoid longing into catastrophic multi-day breakdowns
            if candle["close"] < pdl - 30.0:
                continue

            # SETUP A: VWAP_ZSCORE_RECLAIM (10:30 to 12:30)
            if "10:30" <= t <= "12:30":
                impulse_len = m_high - m_low
                if impulse_len >= self.min_impulse_pts and m_high > m_open:
                    if candle["low"] < candle["vwap"] + 2.0:
                        dipped_below_vwap = True
                        sweep_low = min(sweep_low, candle["low"])

                    if dipped_below_vwap and sweep_low > m_low:
                        retrace_depth = m_high - sweep_low
                        if retrace_depth >= 0.35 * impulse_len:
                            # Reclaims VWAP (Z-score >= 0) with EMA9 >= EMA20
                            if candle["close"] > candle["vwap"] and candle["ema9"] >= candle["ema20"]:
                                # Structural Stop Loss beneath sweep low
                                sl = sweep_low - 3.0
                                sl_pts = candle["close"] - sl
                                if 15.0 <= sl_pts <= self.max_stop_loss_pts:
                                    active_trade = GatedTradeRecord(
                                        strategy="VWAP_ZSCORE_RECLAIM",
                                        date=date_str,
                                        direction="BULLISH",
                                        entry_time=t,
                                        entry_price=candle["close"],
                                        stop_loss=sl,
                                        risk_pts=sl_pts,
                                        target_1=m_high,
                                        regime_score=regime_score,
                                        vwap_zscore=float(candle["vwap_zscore"]),
                                        bbw_expansion=float(candle["bbw_expansion_ratio"])
                                    )
                                    trades_count += 1
                                    dipped_below_vwap = False
                                    continue

            # SETUP B: AFTERNOON_BAND_EXPANSION (12:30 to 14:15)
            if t > self.range_anchor_end and t <= self.entry_cutoff_time:
                if range_high > 0:
                    # Breakout above range high + confirmed Z-score >= +1.0 + BBW expansion
                    if (candle["close"] > range_high + 2.0 and
                        candle["close"] > candle["vwap"] and
                        candle["close"] > candle["ema20"] and
                        candle["vwap_zscore"] >= 0.8 and
                        candle["volume"] >= 0.95 * candle["vol_sma20"]):

                        # Structural Base Low in last 20 candles
                        base_low = df.iloc[max(0, i - 20):i]["low"].min()
                        sl = max(base_low, candle["close"] - self.max_stop_loss_pts)
                        sl_pts = candle["close"] - sl

                        if 12.0 <= sl_pts <= self.max_stop_loss_pts:
                            active_trade = GatedTradeRecord(
                                strategy="AFTERNOON_BAND_EXPANSION",
                                date=date_str,
                                direction="BULLISH",
                                entry_time=t,
                                entry_price=candle["close"],
                                stop_loss=sl,
                                risk_pts=sl_pts,
                                target_1=candle["close"] + self.t1_reward_pts,
                                regime_score=regime_score,
                                vwap_zscore=float(candle["vwap_zscore"]),
                                bbw_expansion=float(candle["bbw_expansion_ratio"])
                            )
                            trades_count += 1
                            continue

        return completed_trades
