# Changelog

All notable changes to the ARES trading system will be documented in this file.

## [Unreleased]

### Added
- **Failed Breakout 6-Point Confidence Upgrades**: Refactored the `FailedBreakoutDetector` scoring to support a 6-point evaluation scale, incorporating active OI growth ($\ge 3.0\%$) and deep close-back penetration ($\ge 5.0$ points). Sets confidence to `HIGH` if score >= 4, else `MEDIUM`.
- **Exhaustion Reversal Dynamic Confidence Scoring**: Upgraded the `ExhaustionDetector` from static `MEDIUM` confidence to a dynamic 4-point scoring scale evaluating extreme volume climaxes, extreme doji body ratios, panic IV spikes, and structural level testing. Sets confidence to `HIGH` if score >= 2, else `MEDIUM`.
- **OI Wall Dynamic Confidence Scoring**: Implemented a 4-point dynamic scoring logic for the `OIWallDetector` setup. Confidence is evaluated dynamically based on wall magnitude, active OI building, strike penetration, and intraday wick rejections. Sets confidence to `HIGH` if score >= 2, else `MEDIUM`.
- **FVG Options Lot Sizing & Delta Strike Selection**: Integrated options contract risk management calculations and delta-based strike selection (`options_math.py`). The system now scans the option chain to select the contract with absolute delta closest to 0.45, fetches available trading balance from Dhan HQ API (`availabelBalance`), suggests lots to trade (taking risk parameters and lot size of 65 into account), formats calculations in Discord signals, and logs them in Supabase under `reasons` and `market_context`.
- **Persistent Position Management**: Introduced a Supabase-backed `PositionManager` to track active trades across sessions. This includes loading daily trades into memory on startup and pushing updates to the database to preserve state.
- **Dynamic Trailing Stops**: Evaluated on every tick against live spot prices. When Target 1 (T1) is reached, the stop loss is automatically trailed to the entry price to secure risk-free trades.
- **Trade Update Alerts**: Extended `alerts.py` with `send_trade_update()`, sending color-coded Discord alerts when a trade hits T1 or gets stopped out.
- **Dynamic Target Selection**: Implemented structural-based profit targets (T1, T2) in `FailedBreakoutDetector` and `ExhaustionDetector`. The system now automatically identifies the next significant support/resistance levels from the option chain and structural data to set realistic exit points.
- **Premium Terminal UI**: Added full ANSI color support to the local console output. Includes a high-visibility startup banner, color-coded signal alerts (Green for Bullish, Red for Bearish), and real-time status tracking for rolling buffer warmup.
- **Alert Timestamps**: Signal reasons now include the exact candle timestamp (e.g., "at 11:53am") for better temporal traceability in Discord and console alerts.
- **Improved Entry Logic**: Added `entry_zone_offset_pts` to the configuration, allowing for a configurable price range for optimal entry around the trigger price.
- **Structural Stop Buffers**: Introduced specific stop-loss buffers for each detector type (`oi_wall_stop_buffer`, `exhaustion_stop_buffer`, `breakout_stop_buffer`) for more granular risk management.
- **Backtesting & PineScript Export**: Integrated a new backtesting suite capable of simulating historical performance. Added a PineScript exporter that generates TradingView-compatible code (v6) for visual strategy validation, including entry/exit markers and profit/loss tables.
- **Signal Tracking IDs**: Added a random 4-digit identifier (e.g., `#0501`) to every generated trade signal. This ID is passed along to subsequent trade updates to help easily track and differentiate multiple signals in Discord and the local console.
- **UI Refinement**: Rounded Previous Day High (PDH) and Low (PDL) values to 2 decimal places in the startup dashboard alert for improved terminal aesthetics and clarity.
- **Analytics Persistence**: Created a `trade_analytics` table and an `AnalyticsLogger` class to capture detailed trade histories, market context, and OI data for monthly performance analysis and ML training.

### Changed
- **Decoupled Option Sizing Details**: Removed the duplicate `Option Sizing` info string from the runtime `reasons` list to clean up Discord and console formats, shifting the SQL/Supabase logger appending to the database persistence layer (`storage.py`).
- **Exhaustion Detector Refactor**: Re-engineered the signal generation logic to prioritize structural levels over fixed offsets, falling back to fixed points only when structural levels are unavailable or too tight.
- **Syncronized Warm-up Thresholds**: Aligned all detector and engine warm-up requirements with a single source of truth in `Settings` to ensure consistent signal scoring.
- **Discord Alert Formatting**: Refined the Discord embed structure using diff blocks for high-contrast color coding and improved scannability. Fixed Discord alert chunking to be "code-block aware," ensuring that long messages split correctly without breaking triple-backtick formatting.
- **Dhan API PDH/PDL Oracle**: Migrated the previous day level fetching from Yahoo Finance to the Dhan API's historical daily data endpoint. This removes the external dependency on Yahoo Finance for price fetching and improves structural level accuracy by using broker-native data.
- **Deterministic Target Sorting**: Refined `FailedBreakoutDetector` and `ExhaustionDetector` to ensure profit targets are always sorted by proximity to the entry price. Target 1 (T1) is now guaranteed to be the closer target.
- **Target Proximity Filtering**: Implemented a minimum 20-point distance requirement for structural profit targets in both detectors. This prevents the system from selecting levels too close to the entry price, ensuring a minimum favorable Risk/Reward ratio for dynamic targets.
- **Improved Error Handling**: Implemented multi-step error extraction for Dhan API responses to handle various failure formats (remarks, data fields, raw strings). Added transient error retries for `fetch_latest_candle` and `fetch_chain`.
- **Discord Alert Routing**: Fixed initialization message routing to ensure it goes to the primary signal channel while heartbeats stay in the health channel.
- **Timezone Normalization**: Standardized all system timestamps across ARES to India Standard Time (IST) for consistent reporting.

### Fixed
- **Fly.io Persistence**: Resolved issues with Docker volume mounts that were interfering with source code visibility. Optimized the structure for persistent trade logging.
- **VWAP Reset Logic**: Fixed a bug where VWAP could persist across sessions; it now strictly resets at 09:15 IST daily.
- **Supabase RLS Permissions**: Resolved "42501: new row violates row-level security policy" errors by documenting and implementing the necessary SQL commands to disable or configure RLS for the `active_trades` and `ares_signals` tables.
- **Redundant Logging**: Removed excessive "warming up" console messages, replacing them with a single "BUFFERS FULL" confirmation once the system is active.
- **Position Manager Resilience**: Added lazy initialization and retry logic to `PositionManager` to ensure active trades are eventually loaded even if the initial Supabase connection fails due to temporary network outages.
- **Supabase Schema Mismatch**: Fixed PGRST204 errors when inserting active trades by adding the missing `signal_id` column to the `active_trades` database table and keeping the application schema synchronized.
