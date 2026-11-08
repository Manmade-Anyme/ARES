"""
ARES POC #2: 90-Day IV-Gated Breakout Backtest & Audit
======================================================
Backtests the IV-Gated Breakout Strategy across all 62 trading sessions
(2026-08-07 to 2026-11-05).
Compares:
1. Baseline Breakout (No IV Filter)
2. IV-Gated Breakout (Native Dhan IV Filter)

Evaluates fakeout rejection accuracy and net edge enhancement.
"""

import os
import glob
import pandas as pd
import numpy as np
import pytz
from typing import List, Dict, Any

from poc.iv_breakout_expansion_poc import (
    IVGatedBreakoutEngine,
    IVBreakoutTradeRecord,
    RejectedBreakoutRecord
)

IST = pytz.timezone("Asia/Kolkata")


def load_cached_data():
    """Loads all 62 cached Spot sessions and their corresponding Dhan IV sessions."""
    spot_files = sorted(glob.glob("scratch/data_cache/*.parquet"))
    spot_sessions = {}
    iv_sessions = {}

    for f in spot_files:
        if "iv_cache" in f:
            continue
        d = os.path.basename(f).replace(".parquet", "")
        df_s = pd.read_parquet(f)
        df_s["dt"] = pd.to_datetime(df_s["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
        df_s["time_str"] = df_s["dt"].dt.strftime("%H:%M")
        spot_sessions[d] = df_s

        iv_path = f"scratch/data_cache/iv_cache/{d}.parquet"
        if os.path.exists(iv_path):
            df_iv = pd.read_parquet(iv_path)
            if "time_str" not in df_iv.columns and "timestamp" in df_iv.columns:
                df_iv["dt"] = pd.to_datetime(df_iv["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
                df_iv["time_str"] = df_iv["dt"].dt.strftime("%H:%M")
            iv_sessions[d] = df_iv
        else:
            iv_sessions[d] = None

    return spot_sessions, iv_sessions


def calc_stats(trades: List[IVBreakoutTradeRecord]) -> Dict[str, Any]:
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "wr": 0.0,
            "pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "pf": 0.0,
            "opt_pnl": 0.0
        }
    pnls = [t.pnl_points for t in trades]
    opt_pnls = [t.option_pnl_pts for t in trades if t.option_pnl_pts != 0.0]
    w = [p for p in pnls if p > 0]
    l = [p for p in pnls if p <= 0]
    wr = len(w) / len(pnls) * 100.0
    pf = abs(sum(w) / sum(l)) if l and sum(l) != 0 else np.nan
    return {
        "trades": len(trades),
        "wins": len(w),
        "losses": len(l),
        "wr": wr,
        "pnl": sum(pnls),
        "avg_win": np.mean(w) if w else 0.0,
        "avg_loss": np.mean(l) if l else 0.0,
        "pf": pf,
        "opt_pnl": sum(opt_pnls) if opt_pnls else 0.0
    }


def main():
    print("=" * 85)
    print("ARES POC #2: 90-DAY IV-GATED BREAKOUT BACKTEST & AUDIT")
    print("Data Source: Dhan API 1-Min Intraday Spot & Native ATM Call IV Feed")
    print("Period: 2026-08-07 to 2026-11-05 (62 Trading Sessions)")
    print("=" * 85)

    spot_sessions, iv_sessions = load_cached_data()
    print(f"[+] Loaded {len(spot_sessions)} Spot Sessions & {sum(1 for v in iv_sessions.values() if v is not None)} IV Sessions.")

    engine_base = IVGatedBreakoutEngine(enable_iv_filter=False)
    engine_iv = IVGatedBreakoutEngine(enable_iv_filter=True)

    base_trades: List[IVBreakoutTradeRecord] = []
    iv_trades: List[IVBreakoutTradeRecord] = []
    all_rejected: List[RejectedBreakoutRecord] = []

    for d, df_s in spot_sessions.items():
        df_iv = iv_sessions.get(d)

        # 1. Run Baseline (No IV Filter)
        t_base, _ = engine_base.run_session(df_s, d, df_iv)
        for t in t_base:
            base_trades.append(t)

        # 2. Run IV-Gated Strategy
        t_iv, rej = engine_iv.run_session(df_s, d, df_iv)
        for t in t_iv:
            iv_trades.append(t)
        for r in rej:
            all_rejected.append(r)

    stats_base = calc_stats(base_trades)
    stats_iv = calc_stats(iv_trades)

    print("\n" + "=" * 85)
    print("PERFORMANCE COMPARISON: BASELINE BREAKOUT VS IV-GATED BREAKOUT")
    print("=" * 85)
    print(f"{'Metric':<28} | {'Baseline (No IV)':<22} | {'IV-Gated Breakout':<22} | {'Difference':<12}")
    print("-" * 85)
    print(f"{'Total Breakout Trades':<28} | {stats_base['trades']:<22} | {stats_iv['trades']:<22} | {stats_iv['trades'] - stats_base['trades']:+d}")
    print(f"{'Winning Trades':<28} | {stats_base['wins']:<22} | {stats_iv['wins']:<22} | {stats_iv['wins'] - stats_base['wins']:+d}")
    print(f"{'Losing Trades':<28} | {stats_base['losses']:<22} | {stats_iv['losses']:<22} | {stats_iv['losses'] - stats_base['losses']:+d}")
    print(f"{'Win Rate (%)':<28} | {stats_base['wr']:<21.1f}% | {stats_iv['wr']:<21.1f}% | {stats_iv['wr'] - stats_base['wr']:+5.1f}%")
    print(f"{'Total Spot PnL':<28} | {stats_base['pnl']:<+21.2f} pts | {stats_iv['pnl']:<+21.2f} pts | {stats_iv['pnl'] - stats_base['pnl']:+6.2f} pts")
    print(f"{'Average Win':<28} | {stats_base['avg_win']:<+21.2f} pts | {stats_iv['avg_win']:<+21.2f} pts | {stats_iv['avg_win'] - stats_base['avg_win']:+6.2f} pts")
    print(f"{'Average Loss':<28} | {stats_base['avg_loss']:<+21.2f} pts | {stats_iv['avg_loss']:<+21.2f} pts | {stats_iv['avg_loss'] - stats_base['avg_loss']:+6.2f} pts")
    print(f"{'Profit Factor':<28} | {stats_base['pf']:<22.2f} | {stats_iv['pf']:<22.2f} | {stats_iv['pf'] - stats_base['pf']:+6.2f}")
    if stats_iv['opt_pnl'] != 0.0:
        print(f"{'Total Option Premium PnL':<28} | {stats_base['opt_pnl']:<+21.2f} pts | {stats_iv['opt_pnl']:<+21.2f} pts | {stats_iv['opt_pnl'] - stats_base['opt_pnl']:+6.2f} pts")

    print("\n" + "=" * 85)
    print("IV-GATED FILTER AUDIT: REJECTED FAKEOUTS & PREVENTED LOSSES")
    print("=" * 85)
    total_screened = len(iv_trades) + len(all_rejected)
    print(f"Total Price Breakout Candidates Screened: {total_screened}")
    print(f"Total Breakouts Rejected by IV Filter: {len(all_rejected)}")
    print(f"Total Breakouts Approved by IV Filter: {len(iv_trades)}")
    
    prevented_losses = [r for r in all_rejected if r.potential_pnl_if_taken <= 0]
    missed_wins = [r for r in all_rejected if r.potential_pnl_if_taken > 0]
    prevented_pnl_loss = abs(sum(r.potential_pnl_if_taken for r in prevented_losses))
    missed_pnl_gain = sum(r.potential_pnl_if_taken for r in missed_wins)

    print(f"  -> Fakeouts Correctly Blocked: {len(prevented_losses)} (Accuracy: {len(prevented_losses)/len(all_rejected)*100:.1f}%)")
    print(f"  -> Capital Protected from Stopped Traps: +{prevented_pnl_loss:.2f} pts")
    print(f"  -> False Negatives (Missed Wins): {len(missed_wins)} (-{missed_pnl_gain:.2f} pts)")
    print(f"  -> NET FILTER ALPHA: +{prevented_pnl_loss - missed_pnl_gain:.2f} pts")

    print("\n" + "-" * 85)
    print("REJECTED TRADES DETAIL LOG:")
    print("-" * 85)
    for r in all_rejected:
        print(f"{r.date} {r.time} | Spot: {r.spot_price:8.2f} | IV: {r.iv:5.2f}% (d1: {r.d_iv_1m:+5.2f}%, d3: {r.d_iv_3m:+5.2f}%) | "
              f"Reason: {r.rejection_reason:<22s} | Prevented PnL: {r.potential_pnl_if_taken:+.2f}")

    print("\n" + "=" * 85)
    print("IV-GATED APPROVED TRADES DETAIL LOG:")
    print("=" * 85)
    for t in iv_trades:
        print(f"{t.date} {t.entry_time} | Entry: {t.entry_price:8.2f} | SL: {t.stop_loss:8.2f} | "
              f"Exit: {t.exit_time} ({t.exit_reason:11s}) @ {t.exit_price:8.2f} | PnL: {t.pnl_points:+6.2f} pts | "
              f"IV: {t.iv_at_entry:5.2f}% (d3: {t.iv_delta_3m:+5.2f}%)")


if __name__ == "__main__":
    main()

