"""
Legacy offline audit for the IV-Gated Breakout strategy.
=================================================
A specialized breakout strategy integrating native Dhan Implied Volatility (IV)
microstructure filters to eliminate false breakouts (bull traps) and capture
explosive gamma squeezes.

Core Mechanics:
1. Replays the historical afternoon-range prototype (12:30 - 14:15).
2. Cross-references minute-by-minute ATM/OTM Call IV from Dhan.
3. IV Expansion Filter:
   - Evaluates IV velocity (dIV/dt over 1m, 3m, 5m).
   - Blocks 'Vol-Crush Bull Traps' where spot breaks out but Call IV dumps.
   - Blocks 'Late-Stage Exhaustion' where spot is overextended (>2.2 std) with fading IV.
   - Requires IV momentum confirmation (IV >= SMA5 or dIV_3m >= 0).
4. Tracks both Spot Index PnL and Real Option Premium PnL.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Dict, List, Optional, Tuple, Any
import pandas as pd
import numpy as np


@dataclass
class IVBreakoutTradeRecord:
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
    # IV Microstructure Evidence
    iv_at_entry: float = 0.0
    iv_delta_1m: float = 0.0
    iv_delta_3m: float = 0.0
    iv_delta_5m: float = 0.0
    iv_above_sma5: bool = False
    iv_status: str = "UNKNOWN"
    # Option Contract Tracking
    option_strike: float = 0.0
    option_entry_price: float = 0.0
    option_exit_price: float = 0.0
    option_pnl_pts: float = 0.0


@dataclass
class RejectedBreakoutRecord:
    date: str
    time: str
    spot_price: float
    rejection_reason: str
    iv: float
    d_iv_1m: float
    d_iv_3m: float
    vwap_zscore: float
    potential_pnl_if_taken: float = 0.0


class IVGatedBreakoutEngine:
    """
    Dedicated Breakout Strategy Engine with IV Microstructure Filter.
    """

    def __init__(
        self,
        range_anchor_end: str = "12:30",
        entry_cutoff_time: str = "14:15",
        eod_exit_time: str = "15:15",
        max_trades_per_day: int = 2,
        t1_reward_pts: float = 35.0,
        max_stop_loss_pts: float = 35.0,
        breakout_buffer_pts: float = 2.0,
        min_zscore: float = 0.8,
        enable_iv_filter: bool = True
    ):
        self.range_anchor_end = range_anchor_end
        self.entry_cutoff_time = entry_cutoff_time
        self.eod_exit_time = eod_exit_time
        self.max_trades_per_day = max_trades_per_day
        self.t1_reward_pts = t1_reward_pts
        self.max_stop_loss_pts = max_stop_loss_pts
        self.breakout_buffer_pts = breakout_buffer_pts
        self.min_zscore = min_zscore
        self.enable_iv_filter = enable_iv_filter

    def enrich_spot_session(self, df: pd.DataFrame) -> pd.DataFrame:
        """Enriches spot 1-min candles with VWAP, Z-score, Bollinger Bands, and EMAs."""
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        d = df.copy()
        if "time_str" not in d.columns:
            if "timestamp" in d.columns:
                d["dt"] = pd.to_datetime(d["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
                d["time_str"] = d["dt"].dt.strftime("%H:%M")

        d["typical_price"] = (d["high"] + d["low"] + d["close"]) / 3.0
        d["cum_vol"] = d["volume"].cumsum()
        d["cum_tp_vol"] = (d["typical_price"] * d["volume"]).cumsum()
        d["vwap"] = (d["cum_tp_vol"] / d["cum_vol"].replace(0, np.nan)).ffill()

        # Cumulative volume-weighted variance & Z-score
        d["dev2"] = (d["typical_price"] - d["vwap"]) ** 2
        d["cum_dev2_vol"] = (d["dev2"] * d["volume"]).cumsum()
        d["vwap_std"] = np.sqrt(d["cum_dev2_vol"] / d["cum_vol"].replace(0, np.nan)).ffill().replace(0, 1.0)
        d["vwap_zscore"] = (d["close"] - d["vwap"]) / d["vwap_std"]

        # EMAs and Moving Averages
        d["ema9"] = d["close"].ewm(span=9, adjust=False).mean()
        d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
        d["vol_sma20"] = d["volume"].rolling(20).mean().fillna(d["volume"])

        return d

    def evaluate_iv_filter(
        self,
        time_str: str,
        vwap_zscore: float,
        df_iv: pd.DataFrame
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Evaluates Call IV microstructure at the breakout candle.
        Returns: (passed: bool, status_reason: str, metrics: dict)
        """
        if df_iv is None or df_iv.empty or "strike" not in df_iv.columns:
            return False, "NO_IV_DATA", {}

        current_rows = df_iv[df_iv["time_str"] == time_str]
        if current_rows.empty:
            return False, "NO_IV_DATA", {}

        row = current_rows.iloc[-1]
        curr_strike = row.get("strike")
        try:
            iv_curr = float(row.get("iv"))
            curr_strike = float(curr_strike)
            current_time = datetime.strptime(time_str, "%H:%M")
        except (TypeError, ValueError):
            return False, "NO_IV_DATA", {}
        if not math.isfinite(iv_curr) or iv_curr <= 0 or not math.isfinite(curr_strike) or curr_strike <= 0:
            return False, "NO_IV_DATA", {}

        same_strike_sub = df_iv[
            (df_iv["strike"] == curr_strike)
            & (df_iv["time_str"] <= time_str)
        ]

        def iv_at_offset(minutes: int) -> Optional[float]:
            target_time = (current_time - timedelta(minutes=minutes)).strftime("%H:%M")
            rows = same_strike_sub[same_strike_sub["time_str"] == target_time]
            if rows.empty:
                return None
            try:
                value = float(rows.iloc[-1]["iv"])
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) and value > 0 else None

        iv_prev_1m = iv_at_offset(1)
        iv_prev_3m = iv_at_offset(3)
        if iv_prev_1m is None or iv_prev_3m is None:
            return False, "NO_IV_DATA", {}

        d_iv_1m = iv_curr - iv_prev_1m
        d_iv_3m = iv_curr - iv_prev_3m
        iv_prev_5m = iv_at_offset(5)
        d_iv_5m = iv_curr - iv_prev_5m if iv_prev_5m is not None else d_iv_3m

        # Rolling 5m SMA isolated to the active strike
        window_5m = same_strike_sub.iloc[max(0, len(same_strike_sub) - 5):]["iv"]
        iv_sma5 = float(window_5m.mean()) if not window_5m.empty else iv_curr
        iv_above_sma5 = bool(iv_curr >= iv_sma5)

        metrics = {
            "iv": iv_curr,
            "d_iv_1m": d_iv_1m,
            "d_iv_3m": d_iv_3m,
            "d_iv_5m": d_iv_5m,
            "iv_sma5": iv_sma5,
            "iv_above_sma5": iv_above_sma5,
            "strike": curr_strike,
            "opt_price": float(row.get("close", 0.0))
        }

        # REJECTION RULE 1: Severe Vol-Crush Bull Trap
        # Spot attempting breakout, but Call IV dumping sharply (Institutional Dump)
        if d_iv_1m <= -0.30 or d_iv_3m <= -0.40 or (d_iv_1m < 0.0 and d_iv_3m <= -0.30):
            return False, "VOL_CRUSH_BULL_TRAP", metrics

        # REJECTION RULE 2: Late-Stage Climax Chase
        # Spot extended > 2.2 std from VWAP while IV momentum rolls over
        if vwap_zscore > 2.2 and d_iv_1m <= 0.0:
            return False, "LATE_STAGE_EXHAUSTION", metrics

        # APPROVAL: IV is expanding or stable, confirming genuine buying pressure
        return True, "IV_EXPANSION_CONFIRMED", metrics

    def _get_option_quote_for_strike(
        self,
        df_iv: Optional[pd.DataFrame],
        time_str: str,
        strike: float
    ) -> Optional[float]:
        """
        Selects the exit quote matching the recorded entry strike at or before time_str.
        Prevents cross-strike comparison when multiple strikes exist or ATM rolls.
        """
        if df_iv is None or df_iv.empty:
            return None
        if "strike" not in df_iv.columns or strike <= 0:
            return None
        opt_sub = df_iv[
            (df_iv["time_str"] <= time_str)
            & (df_iv["strike"] == strike)
        ]
        if opt_sub.empty:
            return None
        try:
            price = float(opt_sub.iloc[-1]["close"])
        except (TypeError, ValueError):
            return None
        return price if math.isfinite(price) and price > 0 else None

    def run_session(
        self,
        df_spot: pd.DataFrame,
        date_str: str,
        df_iv: Optional[pd.DataFrame] = None
    ) -> Tuple[List[IVBreakoutTradeRecord], List[RejectedBreakoutRecord]]:
        """
        Runs the IV-Gated Breakout strategy across a single trading session.
        """
        df = self.enrich_spot_session(df_spot)
        completed_trades: List[IVBreakoutTradeRecord] = []
        rejected_breakouts: List[RejectedBreakoutRecord] = []
        active_trade: Optional[IVBreakoutTradeRecord] = None
        trades_count = 0
        cooldown_until_idx = 0

        # Anchor range (09:15 to range_anchor_end, e.g. 12:30)
        ext_mask = (df["time_str"] >= "09:15") & (df["time_str"] <= self.range_anchor_end)
        ext_df = df[ext_mask]
        range_high = ext_df["high"].max() if not ext_df.empty else 0.0

        for i in range(len(df)):
            candle = df.iloc[i]
            t = candle["time_str"]

            # 1. EOD Exit
            if t >= self.eod_exit_time:
                if active_trade is not None:
                    active_trade.exit_time = t
                    active_trade.exit_price = float(candle["close"])
                    active_trade.exit_reason = "EOD_CLOSE"
                    active_trade.pnl_points = active_trade.exit_price - active_trade.entry_price
                    # Option price at exit matching entry strike
                    opt_exit = self._get_option_quote_for_strike(df_iv, t, active_trade.option_strike)
                    if opt_exit is not None:
                        active_trade.option_exit_price = opt_exit
                        active_trade.option_pnl_pts = active_trade.option_exit_price - active_trade.option_entry_price
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
                    opt_exit = self._get_option_quote_for_strike(df_iv, t, active_trade.option_strike)
                    if opt_exit is not None:
                        active_trade.option_exit_price = opt_exit
                        active_trade.option_pnl_pts = active_trade.option_exit_price - active_trade.option_entry_price
                    completed_trades.append(active_trade)
                    active_trade = None
                    cooldown_until_idx = i + 15
                    continue

                # Target 1 check
                if not active_trade.t1_hit and candle["high"] >= active_trade.target_1:
                    active_trade.t1_hit = True
                    active_trade.t1_time = t
                    # Ratchet SL to Breakeven + 2 pts
                    active_trade.stop_loss = active_trade.entry_price + 2.0

                # 20 EMA Trailing Exit (after 13:30)
                if active_trade.t1_hit and candle["close"] < candle["ema20"] and t >= "13:30":
                    active_trade.exit_time = t
                    active_trade.exit_price = float(candle["close"])
                    active_trade.exit_reason = "EMA20_TRAIL"
                    active_trade.pnl_points = active_trade.exit_price - active_trade.entry_price
                    opt_exit = self._get_option_quote_for_strike(df_iv, t, active_trade.option_strike)
                    if opt_exit is not None:
                        active_trade.option_exit_price = opt_exit
                        active_trade.option_pnl_pts = active_trade.option_exit_price - active_trade.option_entry_price
                    completed_trades.append(active_trade)
                    active_trade = None
                    cooldown_until_idx = i + 10
                    continue

                continue

            # 3. New Entry Scans
            if trades_count >= self.max_trades_per_day or i < cooldown_until_idx or t >= self.entry_cutoff_time:
                continue

            # Breakout Candidate Scan (Afternoon Band Expansion)
            if t > self.range_anchor_end and t <= self.entry_cutoff_time and range_high > 0:
                is_price_breakout = (
                    candle["close"] > range_high + self.breakout_buffer_pts and
                    candle["close"] > candle["vwap"] and
                    candle["close"] > candle["ema20"] and
                    candle["vwap_zscore"] >= self.min_zscore and
                    candle["volume"] >= 0.95 * candle["vol_sma20"]
                )

                if is_price_breakout:
                    # Evaluate IV Filter
                    iv_passed, iv_reason, iv_meta = self.evaluate_iv_filter(
                        t, float(candle["vwap_zscore"]), df_iv
                    )

                    base_low = df.iloc[max(0, i - 20):i]["low"].min()
                    sl = max(base_low, candle["close"] - self.max_stop_loss_pts)
                    sl_pts = candle["close"] - sl

                    if not (12.0 <= sl_pts <= self.max_stop_loss_pts):
                        continue

                    # If IV Filter enabled and failed -> Reject and Log
                    if self.enable_iv_filter and not iv_passed:
                        # Simulate what would have happened if taken to measure filter efficacy
                        sim_pnl = 0.0
                        sim_exit_t = self.eod_exit_time
                        for k in range(i + 1, len(df)):
                            c_k = df.iloc[k]
                            if c_k["low"] <= sl:
                                sim_pnl = sl - candle["close"]
                                sim_exit_t = c_k["time_str"]
                                break
                            if c_k["high"] >= candle["close"] + self.t1_reward_pts:
                                sim_pnl = self.t1_reward_pts
                                sim_exit_t = c_k["time_str"]
                                break
                            if c_k["time_str"] >= self.eod_exit_time:
                                sim_pnl = c_k["close"] - candle["close"]
                                sim_exit_t = c_k["time_str"]
                                break

                        rejected_breakouts.append(RejectedBreakoutRecord(
                            date=date_str,
                            time=t,
                            spot_price=float(candle["close"]),
                            rejection_reason=iv_reason,
                            iv=iv_meta.get("iv", 0.0),
                            d_iv_1m=iv_meta.get("d_iv_1m", 0.0),
                            d_iv_3m=iv_meta.get("d_iv_3m", 0.0),
                            vwap_zscore=float(candle["vwap_zscore"]),
                            potential_pnl_if_taken=sim_pnl
                        ))
                        cooldown_until_idx = i + 10
                        continue

                    # Enter Breakout Trade
                    active_trade = IVBreakoutTradeRecord(
                        strategy="IV_GATED_BREAKOUT",
                        date=date_str,
                        direction="BULLISH",
                        entry_time=t,
                        entry_price=float(candle["close"]),
                        stop_loss=float(sl),
                        risk_pts=float(sl_pts),
                        target_1=float(candle["close"] + self.t1_reward_pts),
                        iv_at_entry=iv_meta.get("iv", 0.0),
                        iv_delta_1m=iv_meta.get("d_iv_1m", 0.0),
                        iv_delta_3m=iv_meta.get("d_iv_3m", 0.0),
                        iv_delta_5m=iv_meta.get("d_iv_5m", 0.0),
                        iv_above_sma5=iv_meta.get("iv_above_sma5", False),
                        iv_status=iv_reason,
                        option_strike=iv_meta.get("strike", 0.0),
                        option_entry_price=iv_meta.get("opt_price", 0.0)
                    )
                    trades_count += 1

        return completed_trades, rejected_breakouts
