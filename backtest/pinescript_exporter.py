"""
Pine Script Exporter
====================

Converts ARES backtest results into a TradingView Pine Script v6 indicator.

The generated script plots:
  - Entry markers (▲ for BULLISH, ▼ for BEARISH)
  - Exit markers (✕ for SL, ◆ for T1/T2, ● for EOD)
  - Horizontal SL / T1 / T2 lines during each active trade
  - Background color tint during active trade periods
  - Summary statistics table in the top-right corner

Usage:
    from backtest.pinescript_exporter import export_pinescript
    export_pinescript(trades, metrics, output_path)

Then copy the .pine file contents into TradingView's Pine Editor.
"""

from pathlib import Path
from datetime import datetime, timedelta
from typing import List

from backtest.metrics import BacktestMetrics
from backtest.trade_simulator import CompletedTrade
from models import Direction


# IST offset for converting local timestamps to UTC (TradingView uses UTC)
IST_OFFSET = timedelta(hours=5, minutes=30)


def _to_unix_ms(dt: datetime) -> int:
    """Convert a naive IST datetime to Unix timestamp in milliseconds (UTC)."""
    utc_dt = dt - IST_OFFSET
    epoch = datetime(1970, 1, 1)
    return int((utc_dt - epoch).total_seconds() * 1000)


def _pine_init_func(name: str, values: list, typ: str = "float") -> str:
    """
    Generate a Pine Script v6 function that creates and populates an array.
    Wrapping in a function avoids CE10295 (main body too long).
    Each array.set line is indented with 4 spaces (inside the function body).
    """
    n = len(values)
    lines = [f"init_{name}() =>"]
    lines.append(f"    {typ}[] _arr = array.new_{typ}({n})")
    for i, v in enumerate(values):
        lines.append(f"    array.set(_arr, {i}, {v})")
    lines.append(f"    _arr")
    return "\n".join(lines)


def export_pinescript(
    trades: List[CompletedTrade],
    metrics: BacktestMetrics,
    output_path: Path,
) -> Path:
    """
    Generate a Pine Script v6 indicator file from backtest trades.

    Args:
        trades:      List of CompletedTrade objects from the simulator.
        metrics:     BacktestMetrics computed by MetricsCalculator.
        output_path: Path to write the .pine file.

    Returns:
        Path to the generated .pine file.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Build trade data ─────────────────────────────────────────────────
    entry_times, exit_times = [], []
    entry_prices, exit_prices = [], []
    sl_prices, t1_prices, t2_prices = [], [], []
    directions, pnls, exit_reasons = [], [], []

    reason_map = {"SL": 0, "T1": 1, "T2": 2, "EOD": 3}

    for t in trades:
        entry_times.append(_to_unix_ms(t.entry_time))
        exit_times.append(_to_unix_ms(t.exit_time))
        entry_prices.append(round(t.entry_price, 2))
        exit_prices.append(round(t.exit_price, 2))
        sl_prices.append(round(t.signal.stop_loss, 2))
        t1_prices.append(round(t.signal.target_1, 2))
        t2_prices.append(round(t.signal.target_2, 2))
        directions.append(1.0 if t.signal.direction == Direction.BULLISH else -1.0)
        pnls.append(round(t.pnl_points, 2))
        exit_reasons.append(float(reason_map.get(t.exit_reason, 3)))

    n = len(trades)

    # Color helpers for metrics
    wr_color = "color.green" if metrics.win_rate >= 50 else "color.red"
    pnl_color = "color.green" if metrics.total_pnl >= 0 else "color.red"
    avg_color = "color.green" if metrics.avg_pnl >= 0 else "color.red"
    exp_color = "color.green" if metrics.expectancy >= 0 else "color.red"

    # ── Generate Pine Script v6 ──────────────────────────────────────────
    # Build init function blocks (wrapped in functions to avoid CE10295)
    arr_funcs = "\n\n".join([
        _pine_init_func("entryTimes", entry_times, "int"),
        _pine_init_func("exitTimes", exit_times, "int"),
        _pine_init_func("entryPrices", entry_prices, "float"),
        _pine_init_func("exitPrices", exit_prices, "float"),
        _pine_init_func("slPrices", sl_prices, "float"),
        _pine_init_func("t1Prices", t1_prices, "float"),
        _pine_init_func("t2Prices", t2_prices, "float"),
        _pine_init_func("dirs", directions, "float"),
        _pine_init_func("pnlPts", pnls, "float"),
        _pine_init_func("exitReasons", exit_reasons, "float"),
    ])

    # Declarations that call the init functions (kept in global scope)
    arr_calls = """var int[]   entryTimes   = init_entryTimes()
var int[]   exitTimes    = init_exitTimes()
var float[] entryPrices  = init_entryPrices()
var float[] exitPrices   = init_exitPrices()
var float[] slPrices     = init_slPrices()
var float[] t1Prices     = init_t1Prices()
var float[] t2Prices     = init_t2Prices()
var float[] dirs         = init_dirs()
var float[] pnlPts       = init_pnlPts()
var float[] exitReasons  = init_exitReasons()"""

    pine = f"""\
// ═══════════════════════════════════════════════════════════════════
// ARES Backtest Visualizer — Auto-generated
// Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} IST
// Period: {metrics.period_start} to {metrics.period_end}
// Total Trades: {n} | Win Rate: {metrics.win_rate}%
// ═══════════════════════════════════════════════════════════════════
//@version=6
indicator("ARES Backtest Signals", overlay=true, max_lines_count=500, max_labels_count=500, max_boxes_count=500)

// ─── Configuration ──────────────────────────────────────────────────
showSLLines    = input.bool(true, "Show Stop Loss Lines",  group="Display")
showT1Lines    = input.bool(true, "Show Target 1 Lines",   group="Display")
showT2Lines    = input.bool(true, "Show Target 2 Lines",   group="Display")
showLabels     = input.bool(true, "Show Trade Labels",     group="Display")
showTable      = input.bool(true, "Show Summary Table",    group="Display")
showBackground = input.bool(true, "Show Trade Background", group="Display")

bullColor = input.color(color.new(color.teal, 0),   "Bullish Color", group="Colors")
bearColor = input.color(color.new(color.red, 0),    "Bearish Color", group="Colors")
slColor   = input.color(color.new(color.red, 30),   "Stop Loss Color", group="Colors")
t1Color   = input.color(color.new(color.green, 30), "Target 1 Color",  group="Colors")
t2Color   = input.color(color.new(color.lime, 30),  "Target 2 Color",  group="Colors")

// ─── Init Functions (array data wrapped in functions to stay under body limit)
{arr_funcs}

// ─── Trade Data ─────────────────────────────────────────────────────
int TOTAL_TRADES = {n}
{arr_calls}

// ─── State tracking ─────────────────────────────────────────────────
var int   activeTrade = -1
var float activeEntry = na
var float activeSL    = na
var float activeT1    = na
var float activeT2    = na
var int   activeDir   = 0
var line  lineSL      = na
var line  lineT1      = na
var line  lineT2      = na

// ─── Per-bar logic ──────────────────────────────────────────────────
barTimeMs = time

// Check entries
for i = 0 to TOTAL_TRADES - 1
    int et = array.get(entryTimes, i)
    if barTimeMs >= et and barTimeMs < et + 300000 and activeTrade != i
        if not na(lineSL)
            line.set_x2(lineSL, bar_index)
        if not na(lineT1)
            line.set_x2(lineT1, bar_index)
        if not na(lineT2)
            line.set_x2(lineT2, bar_index)

        activeTrade := i
        activeEntry := array.get(entryPrices, i)
        activeSL    := array.get(slPrices, i)
        activeT1    := array.get(t1Prices, i)
        activeT2    := array.get(t2Prices, i)
        activeDir   := int(array.get(dirs, i))

        isBull = activeDir == 1
        if showLabels
            pnlVal  = array.get(pnlPts, i)
            pnlTxt  = (pnlVal >= 0 ? "+" : "") + str.tostring(pnlVal, "#.##")
            exitR   = array.get(exitReasons, i)
            exitTxt = exitR == 0 ? "SL" : exitR == 1 ? "T1" : exitR == 2 ? "T2" : "EOD"
            lbl     = (isBull ? "LONG" : "SHORT") + "\\n" + str.tostring(activeEntry, "#.##") + "\\nSL: " + str.tostring(activeSL, "#.##") + "\\nT1: " + str.tostring(activeT1, "#.##") + "\\nP&L: " + pnlTxt + " (" + exitTxt + ")"
            lblClr  = pnlVal >= 0 ? bullColor : bearColor
            label.new(bar_index, isBull ? low - ta.atr(14) * 0.5 : high + ta.atr(14) * 0.5, lbl, style=isBull ? label.style_label_up : label.style_label_down, color=lblClr, textcolor=color.white, size=size.small)

        if showSLLines
            lineSL := line.new(bar_index, activeSL, bar_index + 1, activeSL, color=slColor, style=line.style_dashed, width=1)
        if showT1Lines
            lineT1 := line.new(bar_index, activeT1, bar_index + 1, activeT1, color=t1Color, style=line.style_dotted, width=1)
        if showT2Lines
            lineT2 := line.new(bar_index, activeT2, bar_index + 1, activeT2, color=t2Color, style=line.style_dotted, width=1)

// Extend active lines
if activeTrade >= 0
    if not na(lineSL) and showSLLines
        line.set_x2(lineSL, bar_index)
    if not na(lineT1) and showT1Lines
        line.set_x2(lineT1, bar_index)
    if not na(lineT2) and showT2Lines
        line.set_x2(lineT2, bar_index)

// Check exits
for i = 0 to TOTAL_TRADES - 1
    int xt = array.get(exitTimes, i)
    if barTimeMs >= xt and barTimeMs < xt + 300000 and activeTrade == i
        exitP = array.get(exitPrices, i)
        exitR = int(array.get(exitReasons, i))
        markerClr = array.get(pnlPts, i) >= 0 ? bullColor : bearColor
        if showLabels
            exitLabel = exitR == 0 ? "SL" : exitR == 1 ? "T1" : exitR == 2 ? "T2" : "EOD"
            label.new(bar_index, exitP, exitLabel, style=label.style_circle, color=markerClr, textcolor=color.white, size=size.tiny)

        if not na(lineSL)
            line.set_x2(lineSL, bar_index)
        if not na(lineT1)
            line.set_x2(lineT1, bar_index)
        if not na(lineT2)
            line.set_x2(lineT2, bar_index)

        activeTrade := -1
        lineSL      := na
        lineT1      := na
        lineT2      := na

// ─── Background color during active trades ──────────────────────────
bool inTrade = activeTrade >= 0
tradeColor = activeDir == 1 ? color.new(color.teal, 92) : color.new(color.red, 92)
bgcolor(showBackground and inTrade ? tradeColor : na)

// ─── Summary Table ──────────────────────────────────────────────────
if barstate.islast and showTable
    var table summaryTable = table.new(position.top_right, 2, 14, bgcolor=color.new(color.black, 20), border_width=1, border_color=color.gray)
    table.cell(summaryTable, 0, 0,  "ARES BACKTEST",       text_color=color.aqua,  text_size=size.normal, text_halign=text.align_left)
    table.cell(summaryTable, 1, 0,  "{metrics.period_start} to {metrics.period_end}", text_color=color.white, text_size=size.small)
    table.cell(summaryTable, 0, 1,  "Total Trades",        text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 1,  "{metrics.total_trades}",        text_color=color.white,  text_size=size.small)
    table.cell(summaryTable, 0, 2,  "Win Rate",            text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 2,  "{metrics.win_rate}%",           text_color={wr_color},   text_size=size.small)
    table.cell(summaryTable, 0, 3,  "Total P&L",           text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 3,  "{metrics.total_pnl:+.2f} pts", text_color={pnl_color},  text_size=size.small)
    table.cell(summaryTable, 0, 4,  "Avg P&L",             text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 4,  "{metrics.avg_pnl:+.2f} pts",   text_color={avg_color},  text_size=size.small)
    table.cell(summaryTable, 0, 5,  "Sharpe Ratio",        text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 5,  "{metrics.sharpe_ratio:.2f}",    text_color=color.white,  text_size=size.small)
    table.cell(summaryTable, 0, 6,  "Sortino Ratio",       text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 6,  "{metrics.sortino_ratio:.2f}",   text_color=color.white,  text_size=size.small)
    table.cell(summaryTable, 0, 7,  "Profit Factor",       text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 7,  "{metrics.profit_factor:.2f}",   text_color=color.white,  text_size=size.small)
    table.cell(summaryTable, 0, 8,  "Max Drawdown",        text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 8,  "{metrics.max_drawdown_pts:.2f} pts", text_color=color.red, text_size=size.small)
    table.cell(summaryTable, 0, 9,  "Avg Win",             text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 9,  "{metrics.avg_win:+.2f} pts",    text_color=color.green,  text_size=size.small)
    table.cell(summaryTable, 0, 10, "Avg Loss",            text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 10, "{metrics.avg_loss:+.2f} pts",   text_color=color.red,    text_size=size.small)
    table.cell(summaryTable, 0, 11, "Expectancy",          text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 11, "{metrics.expectancy:+.2f} pts", text_color={exp_color},  text_size=size.small)
    table.cell(summaryTable, 0, 12, "SL / T1 / T2 / EOD", text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 12, "{metrics.exit_breakdown.get('SL', 0)} / {metrics.exit_breakdown.get('T1', 0)} / {metrics.exit_breakdown.get('T2', 0)} / {metrics.exit_breakdown.get('EOD', 0)}", text_color=color.white, text_size=size.small)
    table.cell(summaryTable, 0, 13, "Calmar Ratio",        text_color=color.silver, text_size=size.small,  text_halign=text.align_left)
    table.cell(summaryTable, 1, 13, "{metrics.calmar_ratio:.2f}",    text_color=color.white,  text_size=size.small)
"""

    output_path.write_text(pine, encoding="utf-8")
    return output_path
