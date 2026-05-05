# Configuration & Settings

ARES employs a "fail-fast" paradigm using `pydantic-settings`. All system parameters, API keys, and detector thresholds are defined in `config.py` and loaded from the environment variables (typically from a `.env` file). If a required variable is missing, the system will not start.

## Key Environment Variables

### Asset & Market
- `SECURITY_ID`: The Dhan security ID for the target asset (e.g., "13" for NIFTY 50). Supports legacy `NIFTY_SECURITY_ID`.
- `EXCHANGE_SEGMENT`: The exchange segment (e.g., "IDX_I" for Indices, "MCX_COMM" for MCX). Supports legacy `NIFTY_EXCHANGE`.
- `INSTRUMENT_TYPE`: The instrument type (e.g., "INDEX", "COMDTY").
- `YAHOO_SYMBOL`: The Yahoo Finance symbol used to fetch PDH/PDL dynamically (e.g., "^NSEI", "GOLDM=F").
- `STRIKE_INTERVAL`: The distance between option strikes for the asset (e.g., 50 for Nifty, 100 for some commodities).

### System & Persistence
- `DISCORD_WEBHOOK_URL`: For trade alerts.
- `DISCORD_HEALTH_WEBHOOK_URL`: For system heartbeat (Optional).
- `SUPABASE_URL` and `SUPABASE_KEY`: Persistence credentials.

### Engine Parameters ([[04_Engine]])
- `POLL_INTERVAL_SECONDS`: Usually 60.
- `SIGNAL_COOLDOWN_MINUTES`: Prevents over-trading (e.g., 5).
- `CANDLE_BUFFER_SIZE`: Warmup period for volume moving average (e.g., 30).
- `IV_BUFFER_SIZE`: Rolling IV buffer for crush detection (e.g., 10).

### Detector Thresholds ([[05_Detectors]])
- **Breakout:** `BREAKOUT_CONFIRMATION_CANDLES`, `BREAKOUT_FAILURE_MIN_SCORE`, `BREAKOUT_WEAK_VOLUME_RATIO`, `BREAKOUT_IV_FALLING_THRESHOLD`.
- **OI Wall:** `OI_WALL_MIN_OI` (e.g., 5000000), `OI_WALL_MIN_OI_CHANGE_PCT`, `OI_WALL_APPROACH_DISTANCE`, `OI_WALL_TEST_DISTANCE`.
- **Exhaustion:** `EXHAUSTION_VOLUME_MULTIPLIER` (e.g., 2.5), `EXHAUSTION_BODY_RATIO` (e.g., 0.3), `EXHAUSTION_IV_SPIKE_THRESHOLD`.

### Targets & Zones
- `TARGET_1_PTS`: Points to Target 1 (e.g., 40.0).
- `TARGET_2_PTS`: Points to Target 2 (e.g., 80.0).
- `ENTRY_ZONE_OFFSET_PTS`, `STRIKE_INTERVAL`.
