"""
ARES Backtesting Module
=======================

Provides historical data fetching, trade simulation, and performance
analytics for the ARES signal detection engine.

Usage:
    python run_backtest.py --days 90 --interval 5
"""

from backtest.backtest_engine import BacktestEngine
from backtest.metrics import MetricsCalculator
from backtest.trade_simulator import TradeSimulator

__all__ = ["BacktestEngine", "MetricsCalculator", "TradeSimulator"]
