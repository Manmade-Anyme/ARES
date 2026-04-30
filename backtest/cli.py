"""
Backtest CLI
============
Command-line interface for running ARES backtests.

Usage:
    python run_backtest.py --days 90 --interval 5
    python run_backtest.py --from-date 2025-01-01 --to-date 2025-03-31
    python run_backtest.py --days 30 --interval 15 --slippage 2.0
    python run_backtest.py --use-cache
"""

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the backtest runner."""
    parser = argparse.ArgumentParser(
        prog="run_backtest",
        description="ARES Backtesting Engine — Replay historical data through ARES detectors",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_backtest.py --days 60 --interval 5
  python run_backtest.py --from-date 2025-01-01 --to-date 2025-03-31 --interval 15
  python run_backtest.py --days 30 --use-cache --slippage 1.5
        """,
    )

    # ── Date range ───────────────────────────────────────────────────────
    date_group = parser.add_argument_group("Date Range")
    date_group.add_argument(
        "--days", type=int, default=60,
        help="Number of days to look back from today (default: 60)",
    )
    date_group.add_argument(
        "--from-date", type=str, default=None,
        help="Explicit start date (YYYY-MM-DD). Overrides --days.",
    )
    date_group.add_argument(
        "--to-date", type=str, default=None,
        help="Explicit end date (YYYY-MM-DD). Default: today.",
    )

    # ── Data settings ────────────────────────────────────────────────────
    data_group = parser.add_argument_group("Data")
    data_group.add_argument(
        "--interval", type=str, default="5", choices=["1", "5", "15", "25", "60"],
        help="Intraday candle interval in minutes (default: 5)",
    )
    data_group.add_argument(
        "--use-cache", action="store_true",
        help="Use cached Parquet data if available (skip API fetch)",
    )
    data_group.add_argument(
        "--security-id", type=str, default=None,
        help="Dhan security ID (default: from .env / config)",
    )

    # ── Simulation settings ──────────────────────────────────────────────
    sim_group = parser.add_argument_group("Simulation")
    sim_group.add_argument(
        "--slippage", type=float, default=0.0,
        help="Slippage in points per trade (default: 0)",
    )
    sim_group.add_argument(
        "--commission", type=float, default=0.0,
        help="Round-trip commission in points (default: 0)",
    )
    sim_group.add_argument(
        "--warmup", type=int, default=5,
        help="Warmup candles to skip per day (default: 5)",
    )

    # ── Output ───────────────────────────────────────────────────────────
    out_group = parser.add_argument_group("Output")
    out_group.add_argument(
        "--save-report", action="store_true", default=True,
        help="Save a markdown report to reports/ (default: True)",
    )
    out_group.add_argument(
        "--no-report", action="store_true",
        help="Skip saving the markdown report",
    )
    out_group.add_argument(
        "--export-pine", action="store_true", default=True,
        help="Generate a Pine Script for TradingView (default: True)",
    )
    out_group.add_argument(
        "--no-pine", action="store_true",
        help="Skip Pine Script generation",
    )

    return parser.parse_args()


async def main() -> None:
    """Main async entry point for the backtest CLI."""
    args = parse_args()

    # Lazy imports so --help is fast
    from backtest.historical_fetcher import HistoricalFetcher
    from backtest.data_store import DataStore
    from backtest.backtest_engine import BacktestEngine
    from backtest.trade_simulator import TradeSimulator
    from backtest.metrics import MetricsCalculator
    from backtest.report import print_report, save_markdown_report

    # ── Resolve dates ────────────────────────────────────────────────────
    if args.from_date:
        from_date = args.from_date
    else:
        from_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")

    to_date = args.to_date or datetime.now().strftime("%Y-%m-%d")

    print(f"\n\033[96m{'═' * 70}\033[0m")
    print(f"\033[96m\033[1m  ARES BACKTEST — Starting\033[0m")
    print(f"\033[96m{'═' * 70}\033[0m")
    print(f"  Period    : {from_date} → {to_date}")
    print(f"  Interval  : {args.interval} min")
    print(f"  Slippage  : {args.slippage} pts")
    print(f"  Commission: {args.commission} pts")

    # ── Data Acquisition ─────────────────────────────────────────────────
    store = DataStore()
    fetcher = HistoricalFetcher()
    symbol = args.security_id or "NIFTY"

    # Try cache first
    intraday_df = None
    daily_df = None

    if args.use_cache:
        intraday_df = store.load_raw(symbol, f"{args.interval}min", from_date, to_date)
        daily_df = store.load_raw(symbol, "daily", from_date, to_date)
        if intraday_df is not None:
            print(f"\n\033[92m  ✓ Loaded cached intraday data: {len(intraday_df)} candles\033[0m")
        if daily_df is not None:
            print(f"\033[92m  ✓ Loaded cached daily data: {len(daily_df)} candles\033[0m")

    if intraday_df is None:
        print(f"\n\033[93m  ⏳ Fetching intraday data from Dhan API...\033[0m")
        try:
            intraday_df = await fetcher.fetch_intraday(from_date, to_date, interval=args.interval)
            store.save_raw(intraday_df, symbol, f"{args.interval}min", from_date, to_date)
            print(f"\033[92m  ✓ Fetched {len(intraday_df)} intraday candles\033[0m")
        except Exception as e:
            print(f"\033[91m  ✗ Failed to fetch intraday data: {e}\033[0m")
            sys.exit(1)

    if daily_df is None:
        print(f"\033[93m  ⏳ Fetching daily data from Dhan API...\033[0m")
        try:
            daily_df = await fetcher.fetch_daily(from_date, to_date)
            store.save_raw(daily_df, symbol, "daily", from_date, to_date)
            print(f"\033[92m  ✓ Fetched {len(daily_df)} daily candles\033[0m")
        except Exception as e:
            print(f"\033[93m  ⚠ Daily data fetch failed ({e}). Will compute PDH/PDL from intraday.\033[0m")
            daily_df = None

    if intraday_df.empty:
        print(f"\033[91m  ✗ No intraday data available. Exiting.\033[0m")
        sys.exit(1)

    # ── Clean Data ───────────────────────────────────────────────────────
    print(f"\n\033[93m  🧹 Cleaning data...\033[0m")
    clean_df = DataStore.clean(intraday_df)
    print(f"\033[92m  ✓ Clean dataset: {len(clean_df)} candles "
          f"(removed {len(intraday_df) - len(clean_df)} outliers/dupes)\033[0m")

    # ── Run Backtest Engine ──────────────────────────────────────────────
    print(f"\n\033[93m  🚀 Running backtest engine...\033[0m")
    engine = BacktestEngine()
    signals = engine.run(clean_df, daily_df=daily_df, warmup_candles=args.warmup)
    print(f"\033[92m  ✓ Generated {len(signals)} signals\033[0m")

    if not signals:
        print(f"\033[93m  ⚠ No signals generated during backtest period.\033[0m")
        print(f"\033[93m    This could mean the detectors did not find qualifying setups\033[0m")
        print(f"\033[93m    in the given data. Try a longer period or different interval.\033[0m")
        return

    # ── Simulate Trades ──────────────────────────────────────────────────
    print(f"\033[93m  📊 Simulating trades...\033[0m")
    simulator = TradeSimulator(slippage_pts=args.slippage, commission_pts=args.commission)
    trades = simulator.simulate(signals, clean_df)
    print(f"\033[92m  ✓ Completed {len(trades)} trades\033[0m")

    if not trades:
        print(f"\033[93m  ⚠ No trades completed (signals may be at end of data).\033[0m")
        return

    # ── Calculate Metrics ────────────────────────────────────────────────
    calc = MetricsCalculator()
    metrics = calc.calculate(trades, total_candles=len(clean_df))
    metrics.interval = f"{args.interval} min"

    # ── Print Report ─────────────────────────────────────────────────────
    print_report(metrics, trades)

    # ── Save Report ──────────────────────────────────────────────────────
    report_dir = Path(__file__).resolve().parent.parent / "reports" / "backtest"
    if args.save_report and not args.no_report:
        filepath = save_markdown_report(metrics, trades, report_dir)
        print(f"\033[92m  📄 Report saved: {filepath}\033[0m")

    # ── Export Pine Script ────────────────────────────────────────────────
    if args.export_pine and not args.no_pine:
        from backtest.pinescript_exporter import export_pinescript
        pine_path = report_dir / "ares_signals.pine"
        export_pinescript(trades, metrics, pine_path)
        print(f"\033[92m  📈 Pine Script saved: {pine_path}\033[0m")
        print(f"\033[93m     → Open TradingView > Pine Editor > Paste the file contents > Add to Chart\033[0m\n")


def run():
    """Synchronous wrapper for the async main."""
    asyncio.run(main())
