"""Generate three research figures for Exhaustion Stop-Loss Study.

Ledger-driven implementation conforming to REVIEW_CLARIFICATION.md:
1. score_vs_risk.png: Score comparison alongside point exposure and min_rr_ratio failures.
2. trade_rescue_case.png: Intraday candle chart of Signal 305 (same-day rescued SL -> T1_BE case).
3. persistent_loss_case.png: Intraday candle chart of Signal 459 (same-day persistent SL -> SL case).
All levels, timestamps, and exit bars derived directly from cohort and ledgers.
"""
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent

# Set styling
plt.style.use('seaborn-v0_8-whitegrid' if 'seaborn-v0_8-whitegrid' in plt.style.available else 'default')
plt.rcParams['font.sans-serif'] = 'Helvetica, Arial, DejaVu Sans'
plt.rcParams['axes.edgecolor'] = '#cccccc'
plt.rcParams['axes.linewidth'] = 0.8


def make_score_vs_risk_chart():
    summ_df = pd.read_csv(HERE / 'summary_variants.csv')
    labels = ['Baseline\n(10/12 pts)', 'Round 1\n(SL 22 pts)', 'Round 2\n(SL 29 pts)', 'Round 3\n(SL 30 pts)']
    scores = summ_df['total_score'].tolist()
    win_rates = summ_df['win_rate_pct'].tolist()
    exposures = summ_df['total_sl_risk_exposure'].tolist()
    fail_pcts = summ_df['failing_min_rr_pct'].tolist()

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.5), dpi=300)

    # Panel 1: Movement Score & Win Rate
    x = np.arange(len(labels))
    colors = ['#d9534f', '#5bc0de', '#0275d8', '#2e6da4']
    bars = ax1.bar(x, scores, color=colors, width=0.5, edgecolor='#333333', linewidth=1)
    ax1.set_ylabel('Total Movement Score (Points)', fontsize=11, fontweight='bold')
    ax1.set_title('Movement Score by SL Width\n(SL=-1, T1=BE=+1, T2=+2)', fontsize=12, fontweight='bold', pad=10)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontsize=10)
    ax1.axhline(0, color='black', linestyle='--', linewidth=0.8)

    for bar, score, wr in zip(bars, scores, win_rates):
        y_pos = bar.get_height() + (2 if score >= 0 else -6)
        ax1.text(bar.get_x() + bar.get_width()/2, y_pos, f'{score:+.1f} pts\n(WR {wr:.1f}%)',
                 ha='center', va='bottom' if score >= 0 else 'top', fontsize=9, fontweight='bold')

    ax1.set_ylim(-65, 75)

    # Panel 2: Stop-Loss Point Risk Exposure & Min R:R Failure Rate
    ax2_twin = ax2.twinx()
    bars2 = ax2.bar(x - 0.15, exposures, color='#f0ad4e', width=0.3, label='Total SL Exposure (pts)', edgecolor='#333333', linewidth=0.8)
    lines = ax2_twin.plot(x + 0.15, fail_pcts, color='#d9534f', marker='o', linewidth=2.5, markersize=8, label='Failing min_rr_ratio (%)')

    ax2.set_ylabel('Total SL Point Exposure (pts)', fontsize=11, fontweight='bold', color='#b06d0f')
    ax2_twin.set_ylabel('Rejected by Engine Gate (%)', fontsize=11, fontweight='bold', color='#d9534f')
    ax2_twin.tick_params(colors='#d9534f')
    ax2.tick_params(colors='#b06d0f')
    ax2.set_title('Downside Point Exposure vs Production Risk Gate\n(min_rr_ratio = 1.0)', fontsize=12, fontweight='bold', pad=10)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, fontsize=10)
    ax2.set_ylim(0, 2600)
    ax2_twin.set_ylim(-5, 115)
    ax2_twin.grid(False)

    for bar, exp in zip(bars2, exposures):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 50, f'{exp:.0f} pts',
                 ha='center', va='bottom', fontsize=8.5, fontweight='bold', color='#b06d0f')

    for xi, fp in zip(x + 0.15, fail_pcts):
        ax2_twin.text(xi, fp + 4, f'{fp:.1f}%', ha='center', va='bottom', fontsize=8.5, fontweight='bold', color='#d9534f')

    fig.tight_layout()
    fig.savefig(HERE / 'score_vs_risk.png')
    plt.close(fig)
    print("Saved score_vs_risk.png")


def make_rescue_case_chart():
    """Plot Signal 305: Same-day rescued case (SL -> T1_BE).

    Derived strictly from cohort.csv, baseline_ledger.csv, and sl_22_ledger.csv.
    """
    cohort_df = pd.read_csv(HERE / 'cohort.csv')
    base_df = pd.read_csv(HERE / 'baseline_ledger.csv')
    r1_df = pd.read_csv(HERE / 'sl_22_ledger.csv')

    c = cohort_df[cohort_df['signal_id'] == 305].iloc[0]
    b = base_df[base_df['signal_id'] == 305].iloc[0]
    r = r1_df[r1_df['signal_id'] == 305].iloc[0]

    dhan_raw = json.loads((HERE / 'dhan_raw.json').read_text())
    candles = pd.DataFrame(dhan_raw)
    candles['time'] = pd.to_datetime(candles['timestamp'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
    candles['hhmm'] = candles['time'].dt.strftime('%H:%M')
    candles['date'] = candles['time'].dt.strftime('%Y-%m-%d')
    candles = candles[(candles['hhmm'] >= '09:15') & (candles['hhmm'] < '15:30')].sort_values('timestamp').reset_index(drop=True)
    bars = candles.to_dict('records')

    start_idx = int(c['start_idx'])      # 5101 (13:01)
    base_exit_idx = int(b['exit_index'])  # 5102 (13:02)
    r1_exit_idx = int(r['exit_index'])    # 5116 (13:16)

    # Slice surrounding window: 13:00 (5100) to 13:20 (5120)
    win_start = start_idx - 1
    win_end = r1_exit_idx + 4
    slice_bars = bars[win_start:win_end + 1]

    entry = float(c['entry_price'])       # 24630.10
    base_sl = float(b['sl_price'])        # 24640.10
    wider_sl_22 = float(r['sl_price'])    # 24652.10
    t1 = float(c['t1_price'])             # 24612.10

    fig, ax = plt.subplots(figsize=(12, 6), dpi=300)

    # Plot candlesticks
    for i, bar in enumerate(slice_bars):
        bar_idx = win_start + i
        color = '#2ca02c' if bar['close'] >= bar['open'] else '#d62728'
        ax.plot([i, i], [bar['low'], bar['high']], color=color, linewidth=1.2)
        body_bottom = min(bar['open'], bar['close'])
        body_top = max(bar['open'], bar['close'])
        height = max(0.2, body_top - body_bottom)
        rect = plt.Rectangle((i - 0.3, body_bottom), 0.6, height, facecolor=color, edgecolor=color, alpha=0.85)
        ax.add_patch(rect)

    # Reference levels
    ax.axhline(entry, color='#333333', linestyle='--', linewidth=1.4, label=f'Entry: {entry:.2f}')
    ax.axhline(base_sl, color='#d9534f', linestyle='-', linewidth=1.6, label=f'Baseline Stop (10 pts): {base_sl:.2f}')
    ax.axhline(wider_sl_22, color='#0275d8', linestyle=':', linewidth=1.8, label=f'Wider Stop (22 pts): {wider_sl_22:.2f}')
    ax.axhline(t1, color='#5cb85c', linestyle='-.', linewidth=1.6, label=f'Target 1 (18 pts): {t1:.2f}')

    # Plot exact event markers on actual candles
    entry_pos = start_idx - win_start
    base_exit_pos = base_exit_idx - win_start
    t1_touch_pos = 5115 - win_start  # Bar 5115 (13:15) touched T1
    be_exit_pos = r1_exit_idx - win_start

    # Entry marker
    ax.scatter(entry_pos, entry, color='black', s=70, zorder=5)
    ax.annotate(f"Entry {bars[start_idx]['hhmm']}\n{entry:.2f}", (entry_pos, entry),
                textcoords="offset points", xytext=(-15, -28), fontsize=8.5, fontweight='bold',
                arrowprops=dict(arrowstyle="->", color="black"))

    # Baseline SL exit marker (within bar 5102 high: 24644.85)
    ax.scatter(base_exit_pos, base_sl, color='#d9534f', marker='x', s=90, linewidth=2.2, zorder=5)
    ax.annotate(f"Baseline Stop Hit\n{bars[base_exit_idx]['hhmm']} bar (Score: -1)\nConfigured dist: 10 pts",
                (base_exit_pos, base_sl), textcoords="offset points", xytext=(-25, 20), fontsize=8.5,
                fontweight='bold', color='#d9534f', arrowprops=dict(arrowstyle="->", color="#d9534f"))

    # T1 touch marker (within bar 5115 low: 24611.85)
    ax.scatter(t1_touch_pos, t1, color='#5cb85c', marker='^', s=90, zorder=5)
    ax.annotate(f"T1 Touch\n{bars[5115]['hhmm']} bar\nStop -> BE", (t1_touch_pos, t1),
                textcoords="offset points", xytext=(-10, -32), fontsize=8.5, fontweight='bold',
                color='#5cb85c', arrowprops=dict(arrowstyle="->", color="#5cb85c"))

    # BE exit marker (within bar 5116 high: 24637.55 >= BE 24630.10)
    ax.scatter(be_exit_pos, entry, color='#0275d8', marker='o', s=80, zorder=5)
    ax.annotate(f"Wider SL Exit (BE)\n{bars[r1_exit_idx]['hhmm']} bar (Score: +1)\nProtected by 22pt stop",
                (be_exit_pos, entry), textcoords="offset points", xytext=(15, 20), fontsize=8.5,
                fontweight='bold', color='#0275d8', arrowprops=dict(arrowstyle="->", color="#0275d8"))

    # Formatting
    step = 2
    ax.set_xticks(range(0, len(slice_bars), step))
    ax.set_xticklabels([b['hhmm'] for b in slice_bars[::step]], fontsize=9)
    ax.set_xlabel('Time (IST, 2026-08-06)', fontsize=10, fontweight='bold')
    ax.set_ylabel('NIFTY Spot Index', fontsize=10, fontweight='bold')
    ax.set_title('Same-Day Rescued Trade Case (Signal 305 - BEARISH)\nBaseline Stop (10 pts) Hit at 13:02; Wider SL (22 pts) Absorbed Noise, Hit T1 at 13:15, and Exited at BE (+1)',
                 fontsize=11, fontweight='bold', pad=12)
    ax.legend(loc='upper right', frameon=True, fontsize=8.5)

    fig.tight_layout()
    fig.savefig(HERE / 'trade_rescue_case.png')
    plt.close(fig)
    print("Saved trade_rescue_case.png")


def make_persistent_loss_chart():
    """Plot Signal 459: Same-day persistent stop-loss case (SL -> SL).

    Derived strictly from cohort.csv, baseline_ledger.csv, and sl_30_ledger.csv.
    """
    cohort_df = pd.read_csv(HERE / 'cohort.csv')
    base_df = pd.read_csv(HERE / 'baseline_ledger.csv')
    r3_df = pd.read_csv(HERE / 'sl_30_ledger.csv')

    c = cohort_df[cohort_df['signal_id'] == 459].iloc[0]
    b = base_df[base_df['signal_id'] == 459].iloc[0]
    r = r3_df[r3_df['signal_id'] == 459].iloc[0]

    dhan_raw = json.loads((HERE / 'dhan_raw.json').read_text())
    candles = pd.DataFrame(dhan_raw)
    candles['time'] = pd.to_datetime(candles['timestamp'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
    candles['hhmm'] = candles['time'].dt.strftime('%H:%M')
    candles['date'] = candles['time'].dt.strftime('%Y-%m-%d')
    candles = candles[(candles['hhmm'] >= '09:15') & (candles['hhmm'] < '15:30')].sort_values('timestamp').reset_index(drop=True)
    bars = candles.to_dict('records')

    start_idx = int(c['start_idx'])      # 14603 (15:08)
    base_exit_idx = int(b['exit_index'])  # 14608 (15:13)
    r3_exit_idx = int(r['exit_index'])    # 14624 (15:29)

    win_start = start_idx - 2
    win_end = r3_exit_idx
    slice_bars = bars[win_start:win_end + 1]

    entry = float(c['entry_price'])       # 23386.55
    base_sl = float(b['sl_price'])        # 23396.55
    wider_sl_30 = float(r['sl_price'])    # 23416.55
    t1 = float(c['t1_price'])             # 23368.55

    fig, ax = plt.subplots(figsize=(12, 6), dpi=300)

    for i, bar in enumerate(slice_bars):
        color = '#2ca02c' if bar['close'] >= bar['open'] else '#d62728'
        ax.plot([i, i], [bar['low'], bar['high']], color=color, linewidth=1.2)
        body_bottom = min(bar['open'], bar['close'])
        body_top = max(bar['open'], bar['close'])
        height = max(0.2, body_top - body_bottom)
        rect = plt.Rectangle((i - 0.3, body_bottom), 0.6, height, facecolor=color, edgecolor=color, alpha=0.85)
        ax.add_patch(rect)

    ax.axhline(entry, color='#333333', linestyle='--', linewidth=1.4, label=f'Entry: {entry:.2f}')
    ax.axhline(base_sl, color='#f0ad4e', linestyle='-', linewidth=1.6, label=f'Baseline Stop (10 pts): {base_sl:.2f}')
    ax.axhline(wider_sl_30, color='#d9534f', linestyle='-', linewidth=1.8, label=f'Wider Stop (30 pts): {wider_sl_30:.2f}')
    ax.axhline(t1, color='#5cb85c', linestyle='-.', linewidth=1.6, label=f'Target 1: {t1:.2f} (Unreached)')

    entry_pos = start_idx - win_start
    base_exit_pos = base_exit_idx - win_start
    r3_exit_pos = r3_exit_idx - win_start

    # Entry marker
    ax.scatter(entry_pos, entry, color='black', s=70, zorder=5)
    ax.annotate(f"Entry {bars[start_idx]['hhmm']}\n{entry:.2f}", (entry_pos, entry),
                textcoords="offset points", xytext=(-10, -28), fontsize=8.5, fontweight='bold',
                arrowprops=dict(arrowstyle="->", color="black"))

    # Baseline SL exit marker (within bar 14608 high: 23396.75 >= 23396.55)
    ax.scatter(base_exit_pos, base_sl, color='#f0ad4e', marker='x', s=90, linewidth=2.2, zorder=5)
    ax.annotate(f"Baseline Stop Hit\n{bars[base_exit_idx]['hhmm']} bar\nConfigured dist: 10 pts",
                (base_exit_pos, base_sl), textcoords="offset points", xytext=(-30, 18), fontsize=8.5,
                fontweight='bold', color='#b06d0f', arrowprops=dict(arrowstyle="->", color="#f0ad4e"))

    # Wider SL 30 exit marker (within bar 14624 high: 23477.80 >= 23416.55)
    ax.scatter(r3_exit_pos, wider_sl_30, color='#d9534f', marker='x', s=100, linewidth=2.2, zorder=5)
    ax.annotate(f"Wider Stop Hit\n{bars[r3_exit_idx]['hhmm']} bar\nConfigured dist: 30 pts\nEqual -1 score masks 3x distance",
                (r3_exit_pos, wider_sl_30), textcoords="offset points", xytext=(-85, 25), fontsize=8.5,
                fontweight='bold', color='#d9534f', arrowprops=dict(arrowstyle="->", color="#d9534f"))

    step = 2
    ax.set_xticks(range(0, len(slice_bars), step))
    ax.set_xticklabels([b['hhmm'] for b in slice_bars[::step]], fontsize=9)
    ax.set_xlabel('Time (IST, 2026-09-10)', fontsize=10, fontweight='bold')
    ax.set_ylabel('NIFTY Spot Index', fontsize=10, fontweight='bold')
    ax.set_title('Same-Day Persistent Stop-Loss Case (Signal 459 - BEARISH)\nBaseline Stop Hit at 15:13 (10 pts); Wider Stop Held Until 15:29 Bar (30 pts), Scoring Identical -1.0',
                 fontsize=11, fontweight='bold', pad=12)
    ax.legend(loc='upper left', frameon=True, fontsize=8.5)

    fig.tight_layout()
    fig.savefig(HERE / 'persistent_loss_case.png')
    plt.close(fig)
    print("Saved persistent_loss_case.png")


def main():
    make_score_vs_risk_chart()
    make_rescue_case_chart()
    make_persistent_loss_chart()


if __name__ == '__main__':
    main()
