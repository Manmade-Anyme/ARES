#!/usr/bin/env python3
"""
ARES Backtest Runner
====================

Top-level entry point for running ARES backtests.

Usage:
    python run_backtest.py --days 90 --interval 5
    python run_backtest.py --from-date 2025-01-01 --to-date 2025-03-31
    python run_backtest.py --days 30 --interval 15 --slippage 2.0
    python run_backtest.py --use-cache
    python run_backtest.py --help
"""

import sys
from pathlib import Path

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest.cli import run

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n⏹️ Backtest cancelled by user.")
        sys.exit(0)
