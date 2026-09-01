# ARES API Documentation

This directory contains the auto-generated or consolidated documentation for the core classes and interfaces inside the ARES trading system.

## Ingestion Layer (Fetchers)

### `PriceFetcher` (in `fetchers/price_fetcher.py`)
Responsible for fetching 1-minute intraday candles for the NIFTY 50 index using the DhanHQ API. Maintains session accumulators for calculating the Volume Weighted Average Price (VWAP) incrementally.

**Methods:**
- `reset_vwap() -> None`: Resets cumulative VWAP accumulators to 0. Called at the start of every trading session.
- `fetch_latest_candle() -> OHLCVCandle`: Fetches the most recently completed 1-minute candle from Dhan API and computes the incremental VWAP.
- `fetch_previous_day_ohlc() -> Tuple[float, float]`: Fetches the previous trading day's high and low for the target asset directly from the Dhan API using historical daily data.

### `OIFetcher` (in `fetchers/oi_fetcher.py`)
Fetches the NIFTY option chain from the Dhan API every cycle. It tracks the previous cycle's Open Interest (OI) for each strike and side to compute the OI change percentage dynamically.

**Methods:**
- `get_nearest_expiry() -> str`: Fetches the nearest expiry date from the Dhan API. Result is cached per day.
- `fetch_chain(spot_price: float, expiry: str) -> Tuple[ATMStrikes, List[Dict[str, Any]]]`: Fetches the current option chain, calculates OI changes, and builds ATM strikes and the full chain.

### `LevelFetcher` (in `fetchers/level_fetcher.py`)
Consolidates and formats critical price levels (like PDH, PDL, VWAP) into a standardized `ResistanceLevel` format.

## Options Calculation & Sizing Layer

### `options_math.py`
Provides utility functions to compute risk-managed lot sizes, fetch account balance from Dhan API, select appropriate option strikes based on target delta values, and calculate stop loss and target prices for the option premium.

**Functions:**
- `calculate_risk_amount(capital: float, risk_pct: float) -> float`: Calculates the risk amount based on capital and risk percentage.
- `calculate_points(entry: float, exit: float) -> float`: Returns the absolute price difference.
- `translate_to_premium(points: float, delta: float) -> float`: Translates index points to option premium points based on the delta.
- `calculate_lots(risk_amt: float, sl_points: float, lot_size: int) -> int`: Computes the suggested lot size.
- `calculate_affordable_lots(capital: float, premium: float, lot_size: int) -> int`: Computes the maximum affordable lots.
- `fetch_dhan_capital(dhan_client: Any) -> float`: Asynchronously retrieves available balance from Dhan (availabelBalance field).
- `find_optimal_strike(direction: str, full_chain: list) -> Tuple[Optional[int], Optional[str], Optional[float], Optional[float]]`: Scans the option chain to find the contract with delta absolute value between 0.45 and 0.55, picking the one closest to 0.45.
- `process_fvg_calculation(signal: AresSignal, full_chain: list, dhan_client: Any) -> None`: Coordinates contract selection, capital fetching, and lot/premium SL/target calculations for the generated signal.

## Orchestration Layer (Engine)

### `AresEngine` (in `engine.py`)
The main orchestrator. It maintains rolling state buffers for volume and IV, and strictly enforces signal cooldowns to prevent over-trading.

**Methods:**
- `tick(candle: OHLCVCandle, full_chain: List[Dict], atm: ATMStrikes, iv_change_pct: float, levels: List[ResistanceLevel]) -> Optional[AresSignal]`: Processes a single market cycle. Runs detectors in priority order (Breakout > OI Wall > Exhaustion) and returns a signal if conditions are met and cooldown is clear.

## Detection Layer (Detectors)

### `FailedBreakoutDetector` (in `detectors/breakout.py`)
Tracks "fake-outs" where price crosses a significant level but fails to hold. Stateful detector that monitors breakouts for up to a configurable number of confirmation candles. Now includes structural target selection.

### `OIWallDetector` (in `detectors/oi_wall.py`)
Identifies structural rejection at strikes with massive fresh Open Interest. Stateless detector that checks for price "bounces" or "wick rejections" at key OI levels.

### `ExhaustionDetector` (in `detectors/exhaustion.py`)
Identifies volume climaxes combined with doji-like indecision at price extremes. Uses dynamic target selection based on structural support/resistance.

## Models (in `models.py`)

- `OHLCVCandle`: Dataclass representing a single 1-minute candle (open, high, low, close, volume, vwap, timestamp).
- `OptionRow`: Dataclass for a single option contract (strike, type, ltp, iv, oi, oi_prev, oi_change_pct, gamma, theta).
- `ATMStrikes`: Container for the ATM Call and Put `OptionRow`s and current spot price.
- `ResistanceLevel`: Dataclass defining a support/resistance level (price, source, strength).
- `AresSignal`: Output dataclass containing full trade parameters: `signal_id`, `setup_type`, `direction`, `trigger_price`, `entry_zone`, `stop_loss`, `target_1`, `target_2`, `confidence`, `reasons`, `timestamp`, `strike_to_trade`, and `option_type`.

## Broadcasting Layer

### `alerts.py`
Formats and dispatches Discord notifications asynchronously.

**Functions:**
- `format_signal(signal: AresSignal, spot: float) -> str`: Formats the AresSignal into a clear, scannable Discord message.
- `send_discord(signal: AresSignal, spot: float) -> None`: Dispatches the formatted signal to Discord.
- `send_startup_alert(pdh: float, pdl: float) -> None`: Sends an initialization message to Discord at startup.
- `send_error_alert(error_msg: str) -> None`: Sends system-level error alerts to Discord.
- `send_trade_update(trade: dict, spot: float, update_type: str) -> None`: Sends an alert when an active trade state changes (e.g., T1 Hit, Trailing Stop triggered, SL Hit).

## Persistence Layer (Storage & Analytics)

### `Storage` (in `storage.py`)
Handles persisting ARES signals to a Supabase PostgreSQL database for post-session review and backtesting.

**Methods:**
- `log_signal(signal: AresSignal, spot: float) -> None`: Asynchronously logs a generated signal to the `ares_signals` table without blocking the main event loop.

### `AnalyticsLogger` (in `storage.py`)
Handles permanent storage of trade results and detailed market context in the `trade_analytics` table for post-session analysis and machine learning.

**Methods:**
- `log_entry(trade_id: str, signal: AresSignal, spot: float, atm: Any = None) -> None`: Creates a new entry in `trade_analytics` at the moment a trade is opened, capturing OI data and market context.
- `log_exit(trade_id: str, exit_price: float, final_state: str, pnl_points_override: float | None = None) -> None`: Updates an existing entry with exit details and calculates P&L. `pnl_points_override` is used for `STOPPED_OUT_AT_BE` exits, where the exit fill remains at entry but the stored P&L must reflect the entry-to-T1 profit already locked.

## Position Management Layer

### `PositionManager` (in `position_manager.py`)
Tracks active trades, evaluates trailing stops against live spot prices on every tick, and persists state to Supabase. Now includes lazy initialization to handle temporary network outages.

**Methods:**
- `_initialize_db() -> None`: Fetches active trades from Supabase on startup, purges expired records from previous days, and loads today's trades into memory.
- `add_trade(signal: AresSignal, spot: float, atm: ATMStrikes = None) -> None`: Pushes a new trade into the active memory array, logs entry to `AnalyticsLogger`, and asynchronously logs it to Supabase as 'OPEN'. Every trade is assigned a 4-digit signal tracking ID.
- `update_trades(spot_price: float) -> None`: Iterates over active trades, tracking trailing stops (e.g., trailing SL to entry price once T1 is hit) or stops triggering. For `STOPPED_OUT_AT_BE`, it passes the entry-to-T1 P&L override to `AnalyticsLogger` while preserving the actual exit fill at entry. Broadcasts state changes via Discord and logs exits to `AnalyticsLogger`.

## Analysis Layer (Backtesting)

### `PineScriptExporter` (in `backtest/pinescript_exporter.py`)
Generates a PineScript v6 file for TradingView visualization.

**Methods:**
- `export(trades: List[Dict], filename: str) -> None`: Takes a list of trade dictionaries and generates a formatted PineScript file with entry/exit markers and a performance table.
