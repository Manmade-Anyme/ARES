"""
Report Generator
================
Produces formatted console output and markdown report files for backtest results.
Includes full per-trade log with entry/exit times, SL, targets, and P&L.
"""

from pathlib import Path
from datetime import datetime
from typing import List

from backtest.metrics import BacktestMetrics
from backtest.trade_simulator import CompletedTrade

# ANSI color codes
G = "\033[92m"
Y = "\033[93m"
R = "\033[91m"
C = "\033[96m"
B = "\033[1m"
W = "\033[97m"
DIM = "\033[2m"
RESET = "\033[0m"


def _pnl_color(val: float) -> str:
    """Return ANSI color code based on P&L sign."""
    if val > 0:
        return G
    elif val < 0:
        return R
    return W


def print_report(metrics: BacktestMetrics, trades: List[CompletedTrade]) -> None:
    """Print a rich console report of backtest results with full trade log."""
    print(f"\n{C}{'═' * 70}{RESET}")
    print(f"{C}{B}  ARES BACKTEST REPORT{RESET}")
    print(f"{C}{'═' * 70}{RESET}")
    print(f"  {W}Period    : {B}{metrics.period_start} → {metrics.period_end}{RESET}")
    print(f"  {W}Candles   : {B}{metrics.total_candles:,}{RESET}")
    print(f"  {W}Interval  : {B}{metrics.interval}{RESET}")

    print(f"\n{C}{'─' * 70}{RESET}")
    print(f"  {B}TRADE SUMMARY{RESET}")
    print(f"{C}{'─' * 70}{RESET}")

    pnl_color = G if metrics.total_pnl >= 0 else R
    print(f"  Total Trades    : {B}{metrics.total_trades}{RESET}")
    print(f"  Winners         : {G}{metrics.winning_trades}{RESET}  "
          f"({G}{metrics.win_rate}%{RESET})")
    print(f"  Losers          : {R}{metrics.losing_trades}{RESET}  "
          f"({R}{metrics.loss_rate}%{RESET})")
    print(f"  Breakeven       : {W}{metrics.breakeven_trades}{RESET}")
    print(f"  Total P&L       : {pnl_color}{B}{metrics.total_pnl:+.2f} pts{RESET}")
    print(f"  Avg P&L/Trade   : {pnl_color}{metrics.avg_pnl:+.2f} pts{RESET}")

    print(f"\n{C}{'─' * 70}{RESET}")
    print(f"  {B}RISK METRICS{RESET}")
    print(f"{C}{'─' * 70}{RESET}")
    print(f"  Sharpe Ratio    : {B}{metrics.sharpe_ratio:.2f}{RESET}")
    print(f"  Sortino Ratio   : {B}{metrics.sortino_ratio:.2f}{RESET}")
    print(f"  Calmar Ratio    : {B}{metrics.calmar_ratio:.2f}{RESET}")
    print(f"  Profit Factor   : {B}{metrics.profit_factor:.2f}{RESET}")
    print(f"  Payoff Ratio    : {B}{metrics.payoff_ratio:.2f}{RESET}")
    print(f"  Expectancy      : {B}{metrics.expectancy:+.2f} pts{RESET}")

    print(f"\n{C}{'─' * 70}{RESET}")
    print(f"  {B}DRAWDOWN{RESET}")
    print(f"{C}{'─' * 70}{RESET}")
    print(f"  Max Drawdown    : {R}{metrics.max_drawdown_pts:.2f} pts "
          f"({metrics.max_drawdown_pct:.2f}%){RESET}")
    print(f"  Avg Drawdown    : {Y}{metrics.avg_drawdown_pts:.2f} pts{RESET}")

    print(f"\n{C}{'─' * 70}{RESET}")
    print(f"  {B}WIN / LOSS DETAIL{RESET}")
    print(f"{C}{'─' * 70}{RESET}")
    print(f"  Avg Win         : {G}{metrics.avg_win:+.2f} pts{RESET}")
    print(f"  Avg Loss        : {R}{metrics.avg_loss:+.2f} pts{RESET}")
    print(f"  Largest Win     : {G}{metrics.largest_win:+.2f} pts{RESET}")
    print(f"  Largest Loss    : {R}{metrics.largest_loss:+.2f} pts{RESET}")
    print(f"  Avg MFE         : {G}{metrics.avg_mfe:.2f} pts{RESET}")
    print(f"  Avg MAE         : {R}{metrics.avg_mae:.2f} pts{RESET}")

    print(f"\n{C}{'─' * 70}{RESET}")
    print(f"  {B}EXIT ANALYSIS{RESET}")
    print(f"{C}{'─' * 70}{RESET}")
    for reason, count in sorted(metrics.exit_breakdown.items()):
        pct = count / metrics.total_trades * 100 if metrics.total_trades > 0 else 0
        print(f"  {reason:10s} : {count:4d}  ({pct:.1f}%)")

    print(f"\n{C}{'─' * 70}{RESET}")
    print(f"  {B}HOLDING TIME{RESET}")
    print(f"{C}{'─' * 70}{RESET}")
    print(f"  Avg Holding     : {B}{metrics.avg_holding_candles:.1f} candles{RESET}")
    print(f"  Max Holding     : {B}{metrics.max_holding_candles} candles{RESET}")

    if metrics.detector_breakdown:
        print(f"\n{C}{'─' * 70}{RESET}")
        print(f"  {B}PER-DETECTOR BREAKDOWN{RESET}")
        print(f"{C}{'─' * 70}{RESET}")
        for det, stats in metrics.detector_breakdown.items():
            det_pnl_color = G if stats["total_pnl"] >= 0 else R
            print(f"  {B}{det}{RESET}")
            print(f"    Trades: {int(stats['trades'])}  |  "
                  f"Win Rate: {stats['win_rate']:.1f}%  |  "
                  f"P&L: {det_pnl_color}{stats['total_pnl']:+.2f}{RESET}  |  "
                  f"Avg: {stats['avg_pnl']:+.2f}")

    # ── Full Trade Log ───────────────────────────────────────────────────
    if trades:
        print(f"\n{C}{'═' * 70}{RESET}")
        print(f"{C}{B}  DETAILED TRADE LOG{RESET}")
        print(f"{C}{'═' * 70}{RESET}")

        # Table header
        hdr = (f"  {DIM}{'#':>3}  {'Type':<10} {'Dir':<7} "
               f"{'Entry Time':<17} {'Exit Time':<17} "
               f"{'Entry':>9} {'SL':>9} {'T1':>9} {'T2':>9} "
               f"{'Exit':>9} {'P&L':>8} {'Reason':<5}{RESET}")
        print(hdr)
        print(f"  {DIM}{'─' * 130}{RESET}")

        for i, t in enumerate(trades, 1):
            pc = _pnl_color(t.pnl_points)
            setup = t.signal.setup_type.value[:10]
            dirn = t.signal.direction.value[:7]
            entry_ts = t.entry_time.strftime("%m-%d %H:%M")
            exit_ts = t.exit_time.strftime("%m-%d %H:%M")

            print(
                f"  {W}{i:>3}{RESET}  {C}{setup:<10}{RESET} {dirn:<7} "
                f"{entry_ts:<17} {exit_ts:<17} "
                f"{t.entry_price:>9.2f} {t.signal.stop_loss:>9.2f} "
                f"{t.signal.target_1:>9.2f} {t.signal.target_2:>9.2f} "
                f"{t.exit_price:>9.2f} {pc}{t.pnl_points:>+8.2f}{RESET} {t.exit_reason:<5}"
            )

        print(f"  {DIM}{'─' * 130}{RESET}")

        # Cumulative P&L footer
        cum = 0.0
        wins = sum(1 for t in trades if t.pnl_points > 0)
        total_pnl = sum(t.pnl_points for t in trades)
        pc = _pnl_color(total_pnl)
        print(f"  {B}Total: {len(trades)} trades  |  "
              f"Wins: {wins}  |  Cumulative P&L: {pc}{total_pnl:+.2f} pts{RESET}")

    print(f"\n{C}{'═' * 70}{RESET}\n")


def save_markdown_report(
    metrics: BacktestMetrics,
    trades: List[CompletedTrade],
    output_dir: Path,
) -> Path:
    """
    Save a detailed markdown report with full trade log to the given directory.
    Returns the path to the saved file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = output_dir / f"backtest_report_{ts}.md"

    lines = [
        "# ARES Backtest Report",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Period:** {metrics.period_start} → {metrics.period_end}",
        f"**Total Candles:** {metrics.total_candles:,}",
        "",
        "## Trade Summary",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total Trades | {metrics.total_trades} |",
        f"| Winners | {metrics.winning_trades} ({metrics.win_rate}%) |",
        f"| Losers | {metrics.losing_trades} ({metrics.loss_rate}%) |",
        f"| Total P&L | {metrics.total_pnl:+.2f} pts |",
        f"| Avg P&L | {metrics.avg_pnl:+.2f} pts |",
        "",
        "## Risk Metrics",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Sharpe Ratio | {metrics.sharpe_ratio:.2f} |",
        f"| Sortino Ratio | {metrics.sortino_ratio:.2f} |",
        f"| Calmar Ratio | {metrics.calmar_ratio:.2f} |",
        f"| Profit Factor | {metrics.profit_factor:.2f} |",
        f"| Payoff Ratio | {metrics.payoff_ratio:.2f} |",
        f"| Expectancy | {metrics.expectancy:+.2f} pts |",
        "",
        "## Drawdown",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Max Drawdown | {metrics.max_drawdown_pts:.2f} pts ({metrics.max_drawdown_pct:.2f}%) |",
        f"| Avg Drawdown | {metrics.avg_drawdown_pts:.2f} pts |",
        "",
        "## Win / Loss Detail",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Avg Win | {metrics.avg_win:+.2f} pts |",
        f"| Avg Loss | {metrics.avg_loss:+.2f} pts |",
        f"| Largest Win | {metrics.largest_win:+.2f} pts |",
        f"| Largest Loss | {metrics.largest_loss:+.2f} pts |",
        f"| Avg MFE | {metrics.avg_mfe:.2f} pts |",
        f"| Avg MAE | {metrics.avg_mae:.2f} pts |",
        "",
        "## Exit Analysis",
        "| Exit Type | Count | % |",
        "|-----------|-------|---|",
    ]

    for reason, count in sorted(metrics.exit_breakdown.items()):
        pct = count / metrics.total_trades * 100 if metrics.total_trades > 0 else 0
        lines.append(f"| {reason} | {count} | {pct:.1f}% |")

    if metrics.detector_breakdown:
        lines.extend(["", "## Per-Detector Breakdown",
                       "| Detector | Trades | Win Rate | Total P&L | Avg P&L |",
                       "|----------|--------|----------|-----------|---------|"])
        for det, s in metrics.detector_breakdown.items():
            lines.append(
                f"| {det} | {int(s['trades'])} | {s['win_rate']:.1f}% | "
                f"{s['total_pnl']:+.2f} | {s['avg_pnl']:+.2f} |"
            )

    # ── Full Trade Log ───────────────────────────────────────────────────
    if trades:
        lines.extend([
            "",
            "## Detailed Trade Log",
            "",
            "| # | Setup | Direction | Entry Time | Exit Time | Entry | SL | T1 | T2 | Exit | P&L | Exit Reason | Hold |",
            "|---|-------|-----------|------------|-----------|-------|----|----|----|------|-----|-------------|------|",
        ])
        for i, t in enumerate(trades, 1):
            lines.append(
                f"| {i} "
                f"| {t.signal.setup_type.value} "
                f"| {t.signal.direction.value} "
                f"| {t.entry_time.strftime('%Y-%m-%d %H:%M')} "
                f"| {t.exit_time.strftime('%Y-%m-%d %H:%M')} "
                f"| {t.entry_price:.2f} "
                f"| {t.signal.stop_loss:.2f} "
                f"| {t.signal.target_1:.2f} "
                f"| {t.signal.target_2:.2f} "
                f"| {t.exit_price:.2f} "
                f"| {t.pnl_points:+.2f} "
                f"| {t.exit_reason} "
                f"| {t.holding_candles} |"
            )

    lines.append("")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return filepath
