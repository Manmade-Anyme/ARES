"""
Metrics Calculator
==================
Computes quantitative performance metrics from completed backtest trades.
"""

from dataclasses import dataclass, field
from typing import List, Dict
import math
import numpy as np

from backtest.trade_simulator import CompletedTrade


@dataclass
class BacktestMetrics:
    """Complete performance report for a backtest run."""
    period_start: str = ""
    period_end: str = ""
    interval: str = ""
    total_candles: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    win_rate: float = 0.0
    loss_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    profit_factor: float = 0.0
    payoff_ratio: float = 0.0
    expectancy: float = 0.0
    max_drawdown_pts: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_drawdown_pts: float = 0.0
    avg_mae: float = 0.0
    avg_mfe: float = 0.0
    avg_holding_candles: float = 0.0
    max_holding_candles: int = 0
    exit_breakdown: Dict[str, int] = field(default_factory=dict)
    detector_breakdown: Dict[str, Dict[str, float]] = field(default_factory=dict)
    equity_curve: List[float] = field(default_factory=list)
    drawdown_curve: List[float] = field(default_factory=list)


class MetricsCalculator:
    """Computes all performance metrics from completed trades using numpy."""

    def calculate(self, trades: List[CompletedTrade], total_candles: int = 0) -> BacktestMetrics:
        m = BacktestMetrics(total_candles=total_candles)
        if not trades:
            return m

        trades = sorted(trades, key=lambda t: t.entry_time)
        pnls = np.array([t.pnl_points for t in trades], dtype=np.float64)
        n = len(pnls)

        m.period_start = trades[0].entry_time.strftime("%Y-%m-%d")
        m.period_end = trades[-1].exit_time.strftime("%Y-%m-%d")
        m.total_trades = n
        m.winning_trades = int(np.sum(pnls > 0))
        m.losing_trades = int(np.sum(pnls < 0))
        m.breakeven_trades = int(np.sum(pnls == 0))
        m.win_rate = round(m.winning_trades / n * 100, 2)
        m.loss_rate = round(m.losing_trades / n * 100, 2)
        m.total_pnl = round(float(np.sum(pnls)), 2)
        m.avg_pnl = round(float(np.mean(pnls)), 2)

        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        m.avg_win = round(float(np.mean(wins)), 2) if len(wins) > 0 else 0.0
        m.avg_loss = round(float(np.mean(losses)), 2) if len(losses) > 0 else 0.0
        m.largest_win = round(float(np.max(wins)), 2) if len(wins) > 0 else 0.0
        m.largest_loss = round(float(np.min(losses)), 2) if len(losses) > 0 else 0.0

        gross_profit = float(np.sum(wins)) if len(wins) > 0 else 0.0
        gross_loss = float(np.abs(np.sum(losses))) if len(losses) > 0 else 0.0
        m.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")
        m.payoff_ratio = round(abs(m.avg_win / m.avg_loss), 2) if m.avg_loss != 0 else float("inf")
        m.expectancy = round((m.win_rate / 100 * m.avg_win) + (m.loss_rate / 100 * m.avg_loss), 2)

        if np.std(pnls) > 0:
            m.sharpe_ratio = round(
                float(np.mean(pnls) / np.std(pnls, ddof=1)) * math.sqrt(max(n, 1)), 2)

        downside = pnls[pnls < 0]
        if len(downside) > 0:
            ds = float(np.std(downside, ddof=1))
            if ds > 0:
                m.sortino_ratio = round(float(np.mean(pnls) / ds) * math.sqrt(max(n, 1)), 2)

        equity = np.cumsum(pnls)
        m.equity_curve = [round(float(e), 2) for e in equity]
        running_max = np.maximum.accumulate(equity)
        drawdowns = equity - running_max
        m.drawdown_curve = [round(float(d), 2) for d in drawdowns]
        m.max_drawdown_pts = round(float(np.min(drawdowns)), 2)
        if float(np.max(running_max)) > 0:
            m.max_drawdown_pct = round(float(np.min(drawdowns) / np.max(running_max)) * 100, 2)
        non_zero_dd = drawdowns[drawdowns < 0]
        m.avg_drawdown_pts = round(float(np.mean(non_zero_dd)), 2) if len(non_zero_dd) > 0 else 0.0
        if m.max_drawdown_pts < 0:
            m.calmar_ratio = round(m.total_pnl / abs(m.max_drawdown_pts), 2)

        maes = np.array([t.peak_adverse for t in trades])
        mfes = np.array([t.peak_favorable for t in trades])
        m.avg_mae = round(float(np.mean(maes)), 2)
        m.avg_mfe = round(float(np.mean(mfes)), 2)

        holds = np.array([t.holding_candles for t in trades])
        m.avg_holding_candles = round(float(np.mean(holds)), 1)
        m.max_holding_candles = int(np.max(holds))

        for t in trades:
            m.exit_breakdown[t.exit_reason] = m.exit_breakdown.get(t.exit_reason, 0) + 1

        det_groups: Dict[str, List[float]] = {}
        for t in trades:
            det_groups.setdefault(t.signal.setup_type.value, []).append(t.pnl_points)
        for det, dp in det_groups.items():
            arr = np.array(dp)
            dw = arr[arr > 0]
            dl = arr[arr < 0]
            m.detector_breakdown[det] = {
                "trades": len(arr),
                "win_rate": round(len(dw) / len(arr) * 100, 2) if len(arr) > 0 else 0,
                "total_pnl": round(float(np.sum(arr)), 2),
                "avg_pnl": round(float(np.mean(arr)), 2),
                "avg_win": round(float(np.mean(dw)), 2) if len(dw) > 0 else 0.0,
                "avg_loss": round(float(np.mean(dl)), 2) if len(dl) > 0 else 0.0,
            }
        return m
