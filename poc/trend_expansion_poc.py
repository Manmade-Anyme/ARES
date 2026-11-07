"""
ARES Trend Expansion POC Strategy Module
=========================================
Implements two specialized setups to monetize trend expansion and short-squeeze days:
1. AfternoonBreakoutStrategy (Session High / Range Expansion Breakout):
   - Anchors 09:15 to 12:30 session range.
   - Enters between 12:30 and 14:15 on volume-backed breaks above the multi-hour range.
   - Captures late-session call-unwinding and short-squeeze momentum.

2. VWAPReclaimStrategy (Structural Shakeout & Higher-Low Reversal):
   - Detects >= 60 pt morning impulse legs.
   - Waits for 35-68% Fibonacci liquidity flush below VWAP (38.2% to 66.7% golden ratio zone) that respects the day low.
   - Triggers when price decisively reclaims VWAP with EMA9 >= EMA20 alignment.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np


@dataclass
class TradeRecord:
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
    option_strike: Optional[int] = None
    option_type: Optional[str] = None
    option_entry: Optional[float] = None
    option_exit: Optional[float] = None
    option_pnl: Optional[float] = None


class TrendExpansionEngine:
    """
    Simulates and evaluates the two trend expansion setups on 1-minute intraday data.
    """

    def __init__(
        self,
        range_anchor_end: str = "12:30",
        entry_cutoff_time: str = "14:15",
        eod_exit_time: str = "15:15",
        max_trades_per_day: int = 2,
        t1_reward_pts: float = 35.0,
        max_stop_loss_pts: float = 35.0,
        min_impulse_pts: float = 60.0,
        min_volume_ratio: float = 1.0,
        min_retrace_ratio: float = 0.35,
        max_retrace_ratio: float = 0.68
    ):
        self.range_anchor_end = range_anchor_end
        self.entry_cutoff_time = entry_cutoff_time
        self.eod_exit_time = eod_exit_time
        self.max_trades_per_day = max_trades_per_day
        self.t1_reward_pts = t1_reward_pts
        self.max_stop_loss_pts = max_stop_loss_pts
        self.min_impulse_pts = min_impulse_pts
        self.min_volume_ratio = min_volume_ratio
        self.min_retrace_ratio = min_retrace_ratio
        self.max_retrace_ratio = max_retrace_ratio

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Enrich 1-minute candles with VWAP, EMAs, and ATR."""
        d = df.copy()
        d["typical_price"] = (d["high"] + d["low"] + d["close"]) / 3.0
        d["cum_vol"] = d["volume"].cumsum()
        d["cum_tp_vol"] = (d["typical_price"] * d["volume"]).cumsum()
        d["vwap"] = (d["cum_tp_vol"] / d["cum_vol"].replace(0, np.nan)).ffill()

        d["ema9"] = d["close"].ewm(span=9, adjust=False).mean()
        d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
        d["vol_sma20"] = d["volume"].rolling(20).mean().fillna(d["volume"])

        tr1 = d["high"] - d["low"]
        tr2 = (d["high"] - d["close"].shift(1)).abs()
        tr3 = (d["low"] - d["close"].shift(1)).abs()
        d["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        d["atr14"] = d["tr"].rolling(14).mean().fillna(d["tr"])
        return d

    def run_session(self, df: pd.DataFrame, date_str: str) -> List[TradeRecord]:
        """
        Runs both strategies on a single session's dataframe.
        """
        df = self.compute_indicators(df)
        completed_trades: List[TradeRecord] = []
        active_trade: Optional[TradeRecord] = None
        trades_count = 0
        cooldown_until_idx = 0

        # Morning anchor calculations (09:15 to 10:15)
        m_mask = (df["time_str"] >= "09:15") & (df["time_str"] <= "10:15")
        m_df = df[m_mask]
        m_high = m_df["high"].max() if not m_df.empty else 0
        m_low = m_df["low"].min() if not m_df.empty else 0
        m_open = m_df.iloc[0]["open"] if not m_df.empty else 0

        # Extended anchor calculations (09:15 to range_anchor_end, e.g. 12:30)
        ext_mask = (df["time_str"] >= "09:15") & (df["time_str"] <= self.range_anchor_end)
        ext_df = df[ext_mask]
        range_high = ext_df["high"].max() if not ext_df.empty else 0
        range_low = ext_df["low"].min() if not ext_df.empty else 0

        # State variables for VWAP Reclaim setup
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

            # 2. Position Management for Active Trade
            if active_trade is not None:
                # Stop Loss Check
                if candle["low"] <= active_trade.stop_loss:
                    active_trade.exit_time = t
                    active_trade.exit_price = active_trade.stop_loss
                    active_trade.exit_reason = "SL_HIT"
                    active_trade.pnl_points = active_trade.exit_price - active_trade.entry_price
                    completed_trades.append(active_trade)
                    active_trade = None
                    cooldown_until_idx = i + 15  # 15 candle cooldown after loss
                    dipped_below_vwap = False
                    sweep_low = 999999.0
                    continue

                # Target 1 Check
                if not active_trade.t1_hit and candle["high"] >= active_trade.target_1:
                    active_trade.t1_hit = True
                    active_trade.t1_time = t
                    # Ratchet Stop Loss to Breakeven + 2 pts buffer
                    active_trade.stop_loss = active_trade.entry_price + 2.0

                # 20 EMA Trailing Exit (activated after 13:30 IST)
                if active_trade.t1_hit and candle["close"] < candle["ema20"] and t >= "13:30":
                    active_trade.exit_time = t
                    active_trade.exit_price = candle["close"]
                    active_trade.exit_reason = "EMA20_TRAIL"
                    active_trade.pnl_points = active_trade.exit_price - active_trade.entry_price
                    completed_trades.append(active_trade)
                    active_trade = None
                    cooldown_until_idx = i + 10
                    dipped_below_vwap = False
                    sweep_low = 999999.0
                    continue

                continue

            # 3. New Trade Entry Scanning
            if trades_count >= self.max_trades_per_day or i < cooldown_until_idx or t >= self.entry_cutoff_time:
                continue

            # SETUP A: Structural VWAP Reclaim (Window: 10:30 to 12:30)
            if "10:30" <= t <= "12:30":
                impulse_len = m_high - m_low
                if impulse_len >= self.min_impulse_pts and m_high > m_open:
                    if candle["low"] < candle["vwap"] + 2.0:
                        dipped_below_vwap = True
                        sweep_low = min(sweep_low, candle["low"])

                    if dipped_below_vwap:
                        # Invalidate setup if price flushes beneath morning low
                        if sweep_low <= m_low:
                            dipped_below_vwap = False
                            sweep_low = 999999.0
                        else:
                            retrace_depth = m_high - sweep_low
                            if self.min_retrace_ratio * impulse_len <= retrace_depth <= self.max_retrace_ratio * impulse_len:
                                if candle["close"] > candle["vwap"] and candle["ema9"] >= candle["ema20"]:
                                    sl = sweep_low - 3.0
                                    sl_pts = candle["close"] - sl
                                    if 15.0 <= sl_pts <= self.max_stop_loss_pts:
                                        active_trade = TradeRecord(
                                            strategy="VWAP_RECLAIM",
                                            date=date_str,
                                            direction="BULLISH",
                                            entry_time=t,
                                            entry_price=candle["close"],
                                            stop_loss=sl,
                                            risk_pts=sl_pts,
                                            target_1=m_high
                                        )
                                        trades_count += 1
                                        dipped_below_vwap = False
                                        sweep_low = 999999.0
                                        continue

            # SETUP B: Afternoon Expansion Breakout (Window: 12:30 to 14:15)
            if t > self.range_anchor_end and t <= self.entry_cutoff_time:
                if range_high > 0:
                    if (candle["close"] > range_high + 2.0 and
                        candle["close"] > candle["vwap"] and
                        candle["close"] > candle["ema20"] and
                        candle["volume"] >= self.min_volume_ratio * candle["vol_sma20"]):

                        base_low = df.iloc[max(0, i - 20):i]["low"].min()
                        sl = max(base_low, candle["close"] - self.max_stop_loss_pts)
                        sl_pts = candle["close"] - sl

                        if 12.0 <= sl_pts <= self.max_stop_loss_pts:
                            active_trade = TradeRecord(
                                strategy="AFTERNOON_BREAKOUT",
                                date=date_str,
                                direction="BULLISH",
                                entry_time=t,
                                entry_price=candle["close"],
                                stop_loss=sl,
                                risk_pts=sl_pts,
                                target_1=candle["close"] + self.t1_reward_pts
                            )
                            trades_count += 1
                            continue

        return completed_trades

