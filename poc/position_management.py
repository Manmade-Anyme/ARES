"""Shared position lifecycle rules for the trend-expansion POC engines."""

from dataclasses import dataclass
from typing import Optional, Protocol

import pandas as pd


class ManagedPosition(Protocol):
    entry_price: float
    stop_loss: float
    target_1: float
    t1_hit: bool
    t1_time: Optional[str]
    exit_time: Optional[str]
    exit_price: Optional[float]
    exit_reason: Optional[str]
    pnl_points: float


@dataclass
class ReclaimSetupState:
    dipped_below_vwap: bool = False
    sweep_low: float = 999999.0

    def reset(self) -> None:
        self.dipped_below_vwap = False
        self.sweep_low = 999999.0


@dataclass(frozen=True)
class PositionManagementResult:
    exited: bool
    cooldown_until_idx: int


def manage_long_position(
    trade: ManagedPosition,
    candle: pd.Series,
    candle_index: int,
    cooldown_until_idx: int,
    eod_exit_time: str,
    reclaim_state: ReclaimSetupState,
) -> PositionManagementResult:
    """Apply deterministic, conservative lifecycle rules to one completed candle.

    The stop active before the candle is checked before T1. A stop ratcheted
    after observing T1 is first eligible on the next candle because OHLC data
    does not establish whether the candle's high preceded its low.
    """
    time_str = candle["time_str"]

    if time_str >= eod_exit_time:
        exit_price = candle["close"]
        exit_reason = "EOD_CLOSE"
        next_cooldown = cooldown_until_idx
    elif candle["low"] <= trade.stop_loss:
        exit_price = trade.stop_loss
        exit_reason = "SL_HIT"
        next_cooldown = candle_index + 15
    else:
        exit_price = None
        exit_reason = None
        next_cooldown = cooldown_until_idx

    if exit_reason is not None:
        trade.exit_time = time_str
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason
        trade.pnl_points = exit_price - trade.entry_price
        reclaim_state.reset()
        return PositionManagementResult(True, next_cooldown)

    if not trade.t1_hit and candle["high"] >= trade.target_1:
        trade.t1_hit = True
        trade.t1_time = time_str
        trade.stop_loss = trade.entry_price + 2.0

    if trade.t1_hit and candle["close"] < candle["ema20"] and time_str >= "13:30":
        trade.exit_time = time_str
        trade.exit_price = candle["close"]
        trade.exit_reason = "EMA20_TRAIL"
        trade.pnl_points = trade.exit_price - trade.entry_price
        reclaim_state.reset()
        return PositionManagementResult(True, candle_index + 10)

    return PositionManagementResult(False, next_cooldown)
