# Changelog

All notable changes to the ARES trading system will be documented in this file.

## [Unreleased]

### Added
- **Discord Alerts**: Integrated Discord webhook notifications for system initialization, heartbeat, signal detection, and error alerts. Includes rich embed formatting and color coding.
- **Dynamic PDH/PDL via Yahoo Finance**: Added `yfinance` integration (`httpx`-based asynchronous oracle) to dynamically fetch the Previous Day High (PDH) and Previous Day Low (PDL) during initialization, removing the need for manual hardcoding.
- **Docker & Fly.io Deployment**: Created `Dockerfile` and `fly.toml` for containerized deployment on Fly.io, along with a start script (`start.sh`). Includes GitHub Actions workflow for automated market-schedule-based scaling (up at 09:15 IST, down at 15:25 IST).
- **Error Handling**: Added robust error handling in `PriceFetcher` and `OIFetcher` distinguishing between Dhan API authentication errors, network timeouts, and rate limits. 
- **Docstrings**: Expanded inline documentation across all core engine and fetcher modules.

### Changed
- **Dhan API Configuration**: Updated `Settings` class to align with environment variables (`nifty_security_id` and `nifty_exchange`). Corrected `dhanhq.option_chain()` usage to adhere to API v2 specification (removed invalid `underlying_scrip` parameter).
- **Engine Logic**: The engine now handles API connection failures gracefully with appropriate Discord alerts and retry logic, rather than crashing immediately.

### Fixed
- Fixed persistent "instance refused connection" errors on Fly.io by correctly binding to the expected port or adjusting healthchecks to accommodate a background process without a web server.
- Removed excessive "warming up" console spam for cleaner terminal and Discord logging.
