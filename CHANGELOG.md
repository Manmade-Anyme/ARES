# Changelog

All notable changes to the ARES trading system will be documented in this file.

## [Unreleased]

### Added
- **Dynamic Target Selection**: Implemented structural-based profit targets (T1, T2) in `FailedBreakoutDetector` and `ExhaustionDetector`. The system now automatically identifies the next significant support/resistance levels from the option chain and structural data to set realistic exit points.
- **Premium Terminal UI**: Added full ANSI color support to the local console output. Includes a high-visibility startup banner, color-coded signal alerts (Green for Bullish, Red for Bearish), and real-time status tracking for rolling buffer warmup.
- **Alert Timestamps**: Signal reasons now include the exact candle timestamp (e.g., "at 11:53am") for better temporal traceability in Discord and console alerts.
- **Improved Entry Logic**: Added `entry_zone_offset_pts` to the configuration, allowing for a configurable price range for optimal entry around the trigger price.
- **Structural Stop Buffers**: Introduced specific stop-loss buffers for each detector type (`oi_wall_stop_buffer`, `exhaustion_stop_buffer`, `breakout_stop_buffer`) for more granular risk management.

### Changed
- **Exhaustion Detector Refactor**: Re-engineered the signal generation logic to prioritize structural levels over fixed offsets, falling back to fixed points only when structural levels are unavailable or too tight.
- **Syncronized Warm-up Thresholds**: Aligned all detector and engine warm-up requirements with a single source of truth in `Settings` to ensure consistent signal scoring.
- **Discord Alert Formatting**: Refined the Discord embed structure using diff blocks for high-contrast color coding and improved scannability.

### Fixed
- **Fly.io Persistence**: Resolved issues with Docker volume mounts that were interfering with source code visibility. Optimized the structure for persistent trade logging.
- **VWAP Reset Logic**: Fixed a bug where VWAP could persist across sessions; it now strictly resets at 09:15 IST daily.
- **Redundant Logging**: Removed excessive "warming up" console messages, replacing them with a single "BUFFERS FULL" confirmation once the system is active.
