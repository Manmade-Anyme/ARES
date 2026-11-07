"""
ARES 5th Strategy 90-Day Gated Regime Performance Audit
======================================================
Backtests the Gated Regime Expansion Strategy across all 62 trading sessions
(2026-08-07 to 2026-11-05).
Compares:
1. ARES Core Live (Supabase)
2. Raw POC (No Regime Gate)
3. 5th Strategy (Multi-Factor Volatility Gated)
"""

import os
import glob
import pandas as pd
import numpy as np
from datetime import datetime
import pytz

import storage
from config import settings
from supabase import create_client
from poc.regime_gated_expansion_strategy import GatedRegimeExpansionEngine, GatedTradeRecord
from poc.trend_expansion_poc import TrendExpansionEngine

IST = pytz.timezone("Asia/Kolkata")


def load_cached_sessions_and_pdl(start_date: str = "2026-08-07", end_date: str = "2026-11-05"):
    parquet_files = sorted(glob.glob("scratch/data_cache/*.parquet"))
    sessions = {}
    pdl_map = {}
    prev = None

    for f in parquet_files:
        d = os.path.basename(f).replace(".parquet", "")
        df = pd.read_parquet(f)
        df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
        df["time_str"] = df["dt"].dt.strftime("%H:%M")

        if prev is not None and start_date <= d <= end_date:
            sessions[d] = df
            pdl_map[d] = {
                "pdc": prev["close"],
                "pdh": prev["high"],
                "pdl": prev["low"]
            }

        prev = {
            "open": df.iloc[0]["open"],
            "high": df["high"].max(),
            "low": df["low"].min(),
            "close": df.iloc[-1]["close"]
        }

    return sessions, pdl_map


def load_supabase_ares_trades(start_date: str = "2026-08-07", end_date: str = "2026-11-05") -> pd.DataFrame:
    storage.load_dhan_credentials_from_supabase()
    supabase = create_client(settings.supabase_url, settings.supabase_key)
    res = supabase.table("trade_analytics").select(
        "entry_timestamp, pnl_points, setup_type, result_state"
    ).gte("entry_timestamp", start_date).lte("entry_timestamp", f"{end_date}T23:59:59").execute()

    df_ares = pd.DataFrame(res.data)
    if df_ares.empty:
        return pd.DataFrame()
    df_ares["dt"] = pd.to_datetime(df_ares["entry_timestamp"]).dt.tz_convert(IST)
    df_ares["date"] = df_ares["dt"].dt.strftime("%Y-%m-%d")
    df_ares["pnl_points"] = df_ares["pnl_points"].astype(float)
    df_ares = df_ares[(df_ares["date"] >= start_date) & (df_ares["date"] <= end_date)]
    return df_ares


def main():
    start_date = "2026-08-07"
    end_date = "2026-11-05"

    print("=" * 80)
    print("ARES 5TH STRATEGY: MULTI-FACTOR GATED REGIME AUDIT (90 DAYS)")
    print(f"Period: {start_date} to {end_date} (62 Trading Days)")
    print("=" * 80)

    sessions, pdl_map = load_cached_sessions_and_pdl(start_date=start_date, end_date=end_date)
    audited_dates = sorted(sessions.keys())
    actual_start = audited_dates[0] if audited_dates else start_date
    actual_end = audited_dates[-1] if audited_dates else end_date

    df_ares = load_supabase_ares_trades(start_date=actual_start, end_date=actual_end)
    if not df_ares.empty and audited_dates:
        df_ares = df_ares[df_ares["date"].isin(audited_dates)]
    ares_daily = df_ares.groupby("date")["pnl_points"].sum().to_dict() if not df_ares.empty else {}

    gated_engine = GatedRegimeExpansionEngine()
    raw_engine = TrendExpansionEngine()

    gated_trades: List[GatedTradeRecord] = []
    raw_trades = []
    daily_rows = []

    dormant_days = 0
    active_days = 0

    for d, df in sessions.items():
        if d not in pdl_map:
            continue

        p_info = pdl_map[d]
        pdc = p_info["pdc"]
        pdh = p_info["pdh"]
        pdl = p_info["pdl"]

        # Run Gated 5th Strategy
        is_exp, r_score, factors, _ = gated_engine.evaluate_regime(df, pdc, pdh, pdl)
        day_gated_trades = gated_engine.run_session(df, d, pdc, pdh, pdl)
        
        # Run Raw POC
        day_raw_trades = raw_engine.run_session(df, d)

        if not is_exp:
            dormant_days += 1
        else:
            active_days += 1

        g_pnl = sum(t.pnl_points for t in day_gated_trades)
        r_pnl = sum(t.pnl_points for t in day_raw_trades)
        a_pnl = ares_daily.get(d, 0.0)

        day_range = df["high"].max() - df["low"].min()
        net_move = df.iloc[-1]["close"] - df.iloc[0]["open"]

        daily_rows.append({
            "date": d,
            "range": day_range,
            "net_move": net_move,
            "is_expansion": is_exp,
            "regime_score": r_score,
            "gated_trades": len(day_gated_trades),
            "gated_pnl": g_pnl,
            "raw_trades": len(day_raw_trades),
            "raw_pnl": r_pnl,
            "ares_pnl": a_pnl
        })

        for t in day_gated_trades:
            gated_trades.append(t)
        for t in day_raw_trades:
            raw_trades.append(t)

    df_summary = pd.DataFrame(daily_rows)
    df_summary["month"] = df_summary["date"].str.slice(0, 7)

    # Performance Stats Function
    def calc_stats(trades_list):
        if not trades_list:
            return {"trades": 0, "wins": 0, "losses": 0, "wr": 0.0, "pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "pf": 0.0}
        pnls = [t.pnl_points for t in trades_list]
        w = [p for p in pnls if p > 0]
        l = [p for p in pnls if p <= 0]
        wr = len(w) / len(pnls) * 100
        pf = abs(sum(w) / sum(l)) if l and sum(l) != 0 else np.nan
        return {
            "trades": len(trades_list),
            "wins": len(w),
            "losses": len(l),
            "wr": wr,
            "pnl": sum(pnls),
            "avg_win": np.mean(w) if w else 0.0,
            "avg_loss": np.mean(l) if l else 0.0,
            "pf": pf
        }

    s_gated = calc_stats(gated_trades)
    s_raw = calc_stats(raw_trades)

    ares_pnl = df_ares["pnl_points"].sum() if not df_ares.empty else 0.0
    ares_wr = (len(df_ares[df_ares["pnl_points"] > 0]) / len(df_ares) * 100) if not df_ares.empty else 0.0

    print("\n" + "=" * 80)
    print("3-WAY COMPARISON: ARES CORE VS RAW POC VS 5TH STRATEGY (GATED)")
    print("=" * 80)
    print(f"Total Trading Sessions:       {len(df_summary)}")
    print(f"Regime Gate Active Days:      {active_days} (Strategy Armed)")
    print(f"Regime Gate Dormant Days:     {dormant_days} (Capital Preserved)")
    print("-" * 80)
    print(f"1. ARES Core Live PnL:        {ares_pnl:+9.2f} pts | Trades: {len(df_ares)} | WR: {ares_wr:.1f}%")
    print(f"2. Raw POC PnL (No Gate):     {s_raw['pnl']:+9.2f} pts | Trades: {s_raw['trades']:2d} | WR: {s_raw['wr']:.1f}% | PF: {s_raw['pf']:.2f}")
    print(f"3. 5th Strategy (Gated):      {s_gated['pnl']:+9.2f} pts | Trades: {s_gated['trades']:2d} | WR: {s_gated['wr']:.1f}% | PF: {s_gated['pf']:.2f}")
    print("-" * 80)
    print(f"Gating Net Improvement:       {s_gated['pnl'] - s_raw['pnl']:+9.2f} pts (+{s_gated['wr'] - s_raw['wr']:+.1f}% Win Rate improvement)")

    # Monthly Breakdown
    print("\n" + "=" * 80)
    print("MONTHLY TRAJECTORY COMPARISON")
    print("=" * 80)
    print(f"{'Month':7s} | {'Days':4s} | {'Dormant':7s} | {'5th Trades':10s} | {'5th Win%':8s} | {'5th PnL':10s} | {'Raw PnL':10s} | {'ARES PnL':10s}")
    print("-" * 80)

    for m, grp in df_summary.groupby("month"):
        m_g_trades = [t for t in gated_trades if t.date.startswith(m)]
        m_r_trades = [t for t in raw_trades if t.date.startswith(m)]
        m_g_stats = calc_stats(m_g_trades)
        m_r_stats = calc_stats(m_r_trades)
        m_dormant = len(grp[~grp["is_expansion"]])
        m_ares_pnl = grp["ares_pnl"].sum()

        print(f"{m:7s} | {len(grp):4d} | {m_dormant:7d} | {m_g_stats['trades']:10d} | {m_g_stats['wr']:7.1f}% | {m_g_stats['pnl']:+9.2f} pts | {m_r_stats['pnl']:+9.2f} pts | {m_ares_pnl:+9.2f} pts")

    # Setup Breakdown in 5th Strategy
    print("\n" + "=" * 80)
    print("5TH STRATEGY SETUP BREAKDOWN")
    print("=" * 80)
    for s_name in ["AFTERNOON_BAND_EXPANSION", "VWAP_ZSCORE_RECLAIM"]:
        s_tr = [t for t in gated_trades if t.strategy == s_name]
        st = calc_stats(s_tr)
        print(f"[{s_name}]")
        print(f"  Trades: {st['trades']} | Wins: {st['wins']} | Losses: {st['losses']} | Win Rate: {st['wr']:.1f}%")
        print(f"  Total PnL: {st['pnl']:+.2f} pts | Avg Win: {st['avg_win']:+.2f} | Avg Loss: {st['avg_loss']:+.2f} | PF: {st['pf']:.2f}")


if __name__ == "__main__":
    main()
