"""
ARES Trend Expansion POC Runner & Comparative Backtest
======================================================
Tests the POC strategies across historical ARES sessions.
Pulls real 1-minute candle data directly from Dhan API and evaluates performance.

Usage:
  python3 poc/run_trend_expansion_backtest.py                 # Runs curated Obsidian sessions
  python3 poc/run_trend_expansion_backtest.py --days 90       # Runs past 90 days from Dhan API
  python3 poc/run_trend_expansion_backtest.py --date 2026-11-05 # Runs specific date from Dhan API
"""

import time
import os
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
import pytz

import storage
from config import settings
from dhanhq import dhanhq, DhanContext
from supabase import create_client
from poc.trend_expansion_poc import TrendExpansionEngine, TradeRecord

IST = pytz.timezone("Asia/Kolkata")
CACHE_DIR = "scratch/data_cache"

# Curated sessions documented in Obsidian Daily Market Observation notes
CURATED_SESSIONS = [
    {"date": "2026-10-28", "ares_trades": 1, "ares_pnl": "+20.11", "regime": "Choppy morning -> late bounce"},
    {"date": "2026-10-29", "ares_trades": 3, "ares_pnl": "+84.20", "regime": "Dominant Bullish Expansion (+246 pts)"},
    {"date": "2026-10-30", "ares_trades": 3, "ares_pnl": "-50.80", "regime": "Choppy Friday reversal traps (+88 pts net)"},
    {"date": "2026-11-02", "ares_trades": 2, "ares_pnl": "+4.75",  "regime": "Strong Bullish Expansion (+218 pts)"},
    {"date": "2026-11-03", "ares_trades": 0, "ares_pnl": "0.00",   "regime": "Vertical +370 pt trend (ARES 0 trades)"},
    {"date": "2026-11-04", "ares_trades": 2, "ares_pnl": "-15.15", "regime": "Pre-expiry range chop"},
    {"date": "2026-11-05", "ares_trades": 1, "ares_pnl": "-25.00", "regime": "Trend reversal + PM squeeze (+283 pts)"}
]


def init_dhan():
    storage.load_dhan_credentials_from_supabase()
    context = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
    return dhanhq(context)


def load_supabase_ares_pnl(since_date: str, until_date: Optional[str] = None) -> Dict[str, float]:
    try:
        supabase = create_client(settings.supabase_url, settings.supabase_key)
        q = supabase.table("trade_analytics").select("entry_timestamp, pnl_points").gte("entry_timestamp", since_date)
        if until_date:
            q = q.lte("entry_timestamp", f"{until_date}T23:59:59")
        res = q.execute()
        if not res.data:
            return {}
        df_ares = pd.DataFrame(res.data)
        df_ares["dt"] = pd.to_datetime(df_ares["entry_timestamp"]).dt.tz_convert(IST)
        df_ares["date"] = df_ares["dt"].dt.strftime("%Y-%m-%d")
        df_ares["pnl_points"] = df_ares["pnl_points"].astype(float)
        if until_date:
            df_ares = df_ares[(df_ares["date"] >= since_date) & (df_ares["date"] <= until_date)]
        return df_ares.groupby("date")["pnl_points"].sum().to_dict()
    except Exception as exc:
        print(f"[-] Supabase query error: {exc}")
        return {}


def fetch_nifty_minute_data(dhan, date_str: str, force_refresh: bool = False) -> Optional[pd.DataFrame]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"{date_str}.parquet")

    if not force_refresh and os.path.exists(cache_file):
        df = pd.read_parquet(cache_file)
        if "time_str" not in df.columns:
            df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
            df["time_str"] = df["dt"].dt.strftime("%H:%M")
        return df

    time.sleep(0.2)
    resp = dhan.intraday_minute_data(
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_date=date_str,
        to_date=date_str
    )
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return None
    data = resp.get("data", {})
    if not data or "close" not in data or len(data["close"]) == 0:
        return None

    df = pd.DataFrame(data)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
    df["time_str"] = df["dt"].dt.strftime("%H:%M")
    df.to_parquet(cache_file)
    return df


def run_analysis_for_dates(dates_list: List[Dict[str, str]], dhan, force_refresh: bool = False):
    engine = TrendExpansionEngine()
    all_poc_trades: List[TradeRecord] = []
    session_summaries = []

    earliest_date = dates_list[0]["date"]
    ares_pnl_map = load_supabase_ares_pnl(earliest_date)

    for s_info in dates_list:
        d = s_info["date"]
        df = fetch_nifty_minute_data(dhan, d, force_refresh=force_refresh)
        if df is None:
            continue

        trades = engine.run_session(df, d)
        day_open = df.iloc[0]["open"]
        day_high = df["high"].max()
        day_low = df["low"].min()
        day_close = df.iloc[-1]["close"]
        day_range = day_high - day_low
        net_move = day_close - day_open

        poc_pnl = sum(tr.pnl_points for tr in trades)
        poc_wins = sum(1 for tr in trades if tr.pnl_points > 0)
        poc_losses = sum(1 for tr in trades if tr.pnl_points <= 0)

        ares_val = s_info.get("ares_pnl")
        if ares_val is None:
            ares_val = f"{ares_pnl_map.get(d, 0.0):+.2f}"

        session_summaries.append({
            "date": d,
            "range": day_range,
            "net_move": net_move,
            "ares_pnl": ares_val,
            "poc_trades": len(trades),
            "poc_wins": poc_wins,
            "poc_losses": poc_losses,
            "poc_pnl": poc_pnl,
            "regime": s_info.get("regime", f"Range {day_range:.0f} pts")
        })

        print(f"\n--- {d} | NIFTY Range: {day_range:.1f} pts ({net_move:+.1f} pts) ---")
        print(f"  ARES PnL: {ares_val} pts | POC Trades: {len(trades)} | POC PnL: {poc_pnl:+.2f} pts")

        for tr in trades:
            t1_desc = f"T1 Hit @ {tr.t1_time}" if tr.t1_hit else "T1 Missed"
            print(f"    [{tr.strategy}] Entry: {tr.entry_time} @ {tr.entry_price:.2f} | SL: {tr.stop_loss:.2f} (-{tr.risk_pts:.1f} pts) | Exit: {tr.exit_time} @ {tr.exit_price:.2f} [{tr.exit_reason}] | PnL: {tr.pnl_points:+.2f} pts | {t1_desc}")
            all_poc_trades.append(tr)

    print("\n" + "=" * 80)
    print("AUDIT COMPARISON TABLE (ARES VS POC)")
    print("=" * 80)
    print(f"{'Date':10s} | {'NIFTY Move':12s} | {'ARES PnL':10s} | {'POC PnL':10s} | {'Delta':10s} | Notes")
    print("-" * 80)

    total_ares_pts = 0.0
    total_poc_pts = 0.0

    for s in session_summaries:
        try:
            ares_numeric = float(str(s["ares_pnl"]).replace("+", "").replace(" pts", "").strip())
        except ValueError:
            ares_numeric = 0.0
        poc_val = s["poc_pnl"]
        delta = poc_val - ares_numeric
        total_ares_pts += ares_numeric
        total_poc_pts += poc_val
        print(f"{s['date']:10s} | {s['net_move']:+6.1f} pts   | {ares_numeric:+8.2f} pts | {poc_val:+8.2f} pts | {delta:+8.2f} pts | {s['regime'][:25]}")

    print("-" * 80)
    print(f"{'TOTAL':10s} |              | {total_ares_pts:+8.2f} pts | {total_poc_pts:+8.2f} pts | {total_poc_pts - total_ares_pts:+8.2f} pts |")
    print("=" * 80)

    # Performance stats
    pnls = [t.pnl_points for t in all_poc_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr = len(wins) / len(pnls) * 100 if pnls else 0
    pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else np.nan

    print("\nSUMMARY STATS:")
    print(f"Total POC Trades: {len(all_poc_trades)} | Wins: {len(wins)} | Losses: {len(losses)} | Win Rate: {wr:.1f}%")
    print(f"Total POC PnL:    {sum(pnls):+.2f} pts | Profit Factor: {pf:.2f}")


def main():
    parser = argparse.ArgumentParser(description="ARES Trend Expansion Backtest & Live Dhan Pull")
    parser.add_argument("--days", type=int, default=None, help="Number of historical days to pull from Dhan and analyze (e.g. 90)")
    parser.add_argument("--date", type=str, default=None, help="Specific date to analyze in YYYY-MM-DD format")
    parser.add_argument("--refresh", action="store_true", help="Force refresh data directly from Dhan API bypassing cache")
    args = parser.parse_args()

    print("=" * 80)
    print("ARES POC: LIVE DHAN DATA PULL & ANALYSIS")
    print("=" * 80)

    dhan = init_dhan()

    if args.date:
        dates_list = [{"date": args.date, "regime": f"Single Session ({args.date})"}]
    elif args.days:
        start_date = datetime.now() - timedelta(days=args.days)
        cur = start_date
        dates_list = []
        while cur <= datetime.now():
            if cur.weekday() not in (5, 6): # Skip weekends
                dates_list.append({"date": cur.strftime("%Y-%m-%d")})
            cur += timedelta(days=1)
        print(f"[+] Prepared {len(dates_list)} trading dates across the past {args.days} calendar days.")
    else:
        dates_list = CURATED_SESSIONS

    run_analysis_for_dates(dates_list, dhan, force_refresh=args.refresh)


if __name__ == "__main__":
    main()
