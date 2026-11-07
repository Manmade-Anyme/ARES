"""
ARES 90-Day Market Data Audit & Strategy Backtest
=================================================
Runs the Trend Expansion POC strategy across all 62 trading days in the
past 90 days (2026-08-07 to 2026-11-05).
Compares performance directly against all 309 live/reference trades in Supabase.
"""

import os
import glob
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from datetime import datetime
import pytz

import storage
from config import settings
from supabase import create_client
from poc.trend_expansion_poc import TrendExpansionEngine, TradeRecord

IST = pytz.timezone("Asia/Kolkata")


def load_all_cached_sessions(start_date: str = "2026-08-07", end_date: str = "2026-11-05") -> Dict[str, pd.DataFrame]:
    parquet_files = sorted(glob.glob("scratch/data_cache/*.parquet"))
    sessions = {}
    for f in parquet_files:
        d_str = os.path.basename(f).replace(".parquet", "")
        if start_date <= d_str <= end_date:
            df = pd.read_parquet(f)
            df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
            df["time_str"] = df["dt"].dt.strftime("%H:%M")
            sessions[d_str] = df
    return sessions


def load_supabase_ares_trades(start_date: str = "2026-08-07", end_date: str = "2026-11-05") -> pd.DataFrame:
    storage.load_dhan_credentials_from_supabase()
    supabase = create_client(settings.supabase_url, settings.supabase_key)
    res = supabase.table("trade_analytics").select(
        "entry_timestamp, exit_timestamp, pnl_points, setup_type, result_state, entry_price, exit_price"
    ).gte("entry_timestamp", start_date).lte("entry_timestamp", f"{end_date}T23:59:59").execute()
    
    df_ares = pd.DataFrame(res.data)
    if df_ares.empty:
        return pd.DataFrame()
    
    df_ares["entry_dt"] = pd.to_datetime(df_ares["entry_timestamp"])
    df_ares["date"] = df_ares["entry_dt"].dt.tz_convert(IST).dt.strftime("%Y-%m-%d")
    df_ares["pnl_points"] = df_ares["pnl_points"].astype(float)
    df_ares = df_ares[(df_ares["date"] >= start_date) & (df_ares["date"] <= end_date)]
    return df_ares


def main():
    start_date = "2026-08-07"
    end_date = "2026-11-05"

    print("=" * 80)
    print("STARTING 90-DAY COMPREHENSIVE MARKET AUDIT & BACKTEST")
    print(f"Period: {start_date} to {end_date} (62 Trading Days)")
    print("=" * 80)

    sessions = load_all_cached_sessions(start_date=start_date, end_date=end_date)
    print(f"[+] Loaded {len(sessions)} trading sessions from cache.")

    session_dates = sorted(sessions.keys())
    actual_start = session_dates[0] if session_dates else start_date
    actual_end = session_dates[-1] if session_dates else end_date

    df_ares = load_supabase_ares_trades(start_date=actual_start, end_date=actual_end)
    if not df_ares.empty and session_dates:
        df_ares = df_ares[df_ares["date"].isin(session_dates)]
    ares_daily = df_ares.groupby("date")["pnl_points"].sum().to_dict() if not df_ares.empty else {}
    print(f"[+] Loaded {len(df_ares)} ARES trades from Supabase across {len(ares_daily)} active days.")

    engine = TrendExpansionEngine()
    all_poc_trades: List[TradeRecord] = []
    daily_results = []

    for d_str, df in sessions.items():
        trades = engine.run_session(df, d_str)
        day_open = df.iloc[0]["open"]
        day_high = df["high"].max()
        day_low = df["low"].min()
        day_close = df.iloc[-1]["close"]
        day_range = day_high - day_low
        net_move = day_close - day_open

        poc_pnl = sum(t.pnl_points for t in trades)
        poc_wins = sum(1 for t in trades if t.pnl_points > 0)
        poc_losses = sum(1 for t in trades if t.pnl_points <= 0)

        ares_pnl = ares_daily.get(d_str, 0.0)

        daily_results.append({
            "date": d_str,
            "open": day_open,
            "high": day_high,
            "low": day_low,
            "close": day_close,
            "range": day_range,
            "net_move": net_move,
            "poc_trades": len(trades),
            "poc_wins": poc_wins,
            "poc_losses": poc_losses,
            "poc_pnl": poc_pnl,
            "ares_pnl": ares_pnl
        })

        for tr in trades:
            all_poc_trades.append(tr)

    # Convert to DataFrames for rigorous analytics
    df_daily = pd.DataFrame(daily_results)
    df_daily["month"] = df_daily["date"].str.slice(0, 7)
    df_daily["cum_poc_pnl"] = df_daily["poc_pnl"].cumsum()
    df_daily["cum_ares_pnl"] = df_daily["ares_pnl"].cumsum()

    # Drawdown calculations
    df_daily["poc_peak"] = df_daily["cum_poc_pnl"].cummax()
    df_daily["poc_dd"] = df_daily["cum_poc_pnl"] - df_daily["poc_peak"]
    max_poc_dd = df_daily["poc_dd"].min()

    df_daily["ares_peak"] = df_daily["cum_ares_pnl"].cummax()
    df_daily["ares_dd"] = df_daily["cum_ares_pnl"] - df_daily["ares_peak"]
    max_ares_dd = df_daily["ares_dd"].min()

    # Trade-level statistics
    poc_pnls = [t.pnl_points for t in all_poc_trades]
    wins = [p for p in poc_pnls if p > 0]
    losses = [p for p in poc_pnls if p <= 0]
    wr = len(wins) / len(poc_pnls) * 100 if poc_pnls else 0
    total_poc_pnl = sum(poc_pnls)
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else np.nan

    ares_wr = (len(df_ares[df_ares["pnl_points"] > 0]) / len(df_ares) * 100) if not df_ares.empty else 0.0
    rr_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0
    poc_daily_rate = (len(all_poc_trades) / len(sessions)) if sessions else 0.0
    ares_daily_rate = (len(df_ares) / len(sessions)) if sessions else 0.0

    print("\n" + "=" * 80)
    print("90-DAY STRATEGY COMPARISON: POC VS ARES CORE")
    print("=" * 80)
    print(f"Total Trading Days: {len(sessions)}")
    print(f"POC Total Trades:   {len(all_poc_trades)} ({poc_daily_rate:.1f} / day)")
    print(f"ARES Total Trades:  {len(df_ares)} ({ares_daily_rate:.1f} / day)")
    print("-" * 80)
    print(f"POC Win Rate:       {wr:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"ARES Win Rate:      {ares_wr:.1f}%")
    print("-" * 80)
    print(f"POC Total PnL:      {total_poc_pnl:+.2f} pts")
    print(f"ARES Total PnL:     {df_ares['pnl_points'].sum() if not df_ares.empty else 0.0:+.2f} pts")
    print("-" * 80)
    print(f"POC Avg Win / Loss: {avg_win:+.2f} pts / {avg_loss:+.2f} pts (RR: {rr_ratio:.2f}:1)")
    print(f"POC Profit Factor:  {profit_factor:.2f}")
    print(f"POC Max Drawdown:   {max_poc_dd:.2f} pts")
    print(f"ARES Max Drawdown:  {max_ares_dd:.2f} pts")

    # Monthly Breakdown
    print("\n" + "=" * 80)
    print("MONTHLY BREAKDOWN")
    print("=" * 80)
    print(f"{'Month':7s} | {'Days':4s} | {'POC Trades':10s} | {'POC Win%':8s} | {'POC PnL':10s} | {'ARES PnL':10s} | {'Delta':10s}")
    print("-" * 80)

    for m, m_group in df_daily.groupby("month"):
        m_trades = [t for t in all_poc_trades if t.date.startswith(m)]
        m_pnls = [t.pnl_points for t in m_trades]
        m_wins = [p for p in m_pnls if p > 0]
        m_wr = len(m_wins) / len(m_trades) * 100 if m_trades else 0
        m_poc_pnl = sum(m_pnls)
        m_ares_pnl = m_group["ares_pnl"].sum()
        m_delta = m_poc_pnl - m_ares_pnl
        print(f"{m:7s} | {len(m_group):4d} | {len(m_trades):10d} | {m_wr:7.1f}% | {m_poc_pnl:+9.2f} pts | {m_ares_pnl:+9.2f} pts | {m_delta:+9.2f} pts")

    # Setup Breakdown
    print("\n" + "=" * 80)
    print("POC SETUP BREAKDOWN")
    print("=" * 80)
    for s_name in ["AFTERNOON_BREAKOUT", "VWAP_RECLAIM"]:
        s_trades = [t for t in all_poc_trades if t.strategy == s_name]
        s_pnls = [t.pnl_points for t in s_trades]
        s_wins = [p for p in s_pnls if p > 0]
        s_losses = [p for p in s_pnls if p <= 0]
        s_wr = len(s_wins) / len(s_trades) * 100 if s_trades else 0
        s_tot = sum(s_pnls)
        s_win_avg = np.mean(s_wins) if s_wins else 0
        s_loss_avg = np.mean(s_losses) if s_losses else 0
        s_pf = abs(sum(s_wins) / sum(s_losses)) if s_losses and sum(s_losses) != 0 else np.nan
        print(f"[{s_name}]")
        print(f"  Trades: {len(s_trades)} | Wins: {len(s_wins)} | Losses: {len(s_losses)} | Win Rate: {s_wr:.1f}%")
        print(f"  Total PnL: {s_tot:+.2f} pts | Avg Trade: {np.mean(s_pnls):+.2f} pts")
        print(f"  Avg Win: {s_win_avg:+.2f} pts | Avg Loss: {s_loss_avg:+.2f} pts | Profit Factor: {s_pf:.2f}")

    # Complementarity / Correlation Analysis
    # How does POC perform on days where ARES lost money?
    ares_losing_days = df_daily[df_daily["ares_pnl"] < 0]
    poc_pnl_on_ares_losses = ares_losing_days["poc_pnl"].sum()
    print("\n" + "=" * 80)
    print("COMPLEMENTARITY & HEDGING ANALYSIS")
    print("=" * 80)
    print(f"Days where ARES was Negative: {len(ares_losing_days)} days")
    print(f"ARES Loss on those days:      {ares_losing_days['ares_pnl'].sum():.2f} pts")
    print(f"POC PnL on those exact days:  {poc_pnl_on_ares_losses:+.2f} pts")
    if poc_pnl_on_ares_losses > 0:
        print(f"-> POC provided a positive offset of {poc_pnl_on_ares_losses:.2f} pts during ARES drawdown sessions!")


if __name__ == "__main__":
    main()
