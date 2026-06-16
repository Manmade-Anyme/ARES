# ARES (Adaptive Reversal & Entry Signal) 🚀

**ARES** is a high-performance, asynchronous algorithmic trading signal system designed specifically for **NIFTY 50 options scalping**. 

It continuously monitors 1-minute price action, Option Interest (OI), and Implied Volatility (IV) to identify high-probability reversal and continuation setups, alerting the trader in real-time.

---

## 🏗️ High-Level Architecture

ARES follows a strict **five-layer architecture** designed for modularity, performance, and fault tolerance.

1.  **🧩 Ingestion Layer:** Asynchronous fetchers (`PriceFetcher`, `OIFetcher`) poll the DhanHQ API for 1-min candles, real-time Option Chains, and previous day OHLC levels.
2.  **⚙️ Orchestration Layer:** The `AresEngine` manages the evaluation pipeline, maintaining rolling state buffers (Volume, IV) and enforcing signal cooldowns.
3.  **🔬 Detection Layer:** A suite of specialized detectors (`FailedBreakout`, `OIWall`, `Exhaustion`) score market conditions against technical and structural levels.
4.  **💾 Persistence Layer:** All generated signals and **active trade states** are logged to **Supabase (PostgreSQL)** for post-session performance auditing and backtesting.
5.  **📢 Broadcasting Layer:** Signals are formatted into rich, scannable alerts and dispatched via **Discord Webhooks** and the local console.
6.  **📊 Analysis Layer:** A specialized `backtest/` suite allows for historical simulation and visual export of signals to TradingView via PineScript.

---

## 🎯 Detection Strategies

ARES evaluates three distinct market phenomena in strict **short-circuit priority order**:

### 1. 🚨 Failed Breakout (Highest Priority)
*   **Logic**: Tracks "fake-outs" where price crosses a significant level (PDH/PDL fetched dynamically from Dhan Historical API or massive OI wall) but fails to hold.
*   **Dynamic Targets:** Automatically sets Profit Targets (T1/T2) at the next available structural support/resistance levels.
*   **Scoring:** Evaluated on a 4-point scale:
    *   `Closed Back`: Price returned past the level (Required).
    *   `Weak Volume`: Breakout candle volume < Rolling Average.
    *   `IV Crush`: Dropping Implied Volatility during the cross.
    *   `OI Defense`: Option writers held or increased their positions.
*   **Direction:** Fully bidirectional (handles both Bullish and Bearish failures).

### 2. 🧱 OI Wall Rejection (High Priority)
*   **Logic:** Identifies structural rejection at strikes with massive fresh Open Interest.
*   **Confirmation:** Detects price "bounces" or "wick rejections" when the spot price tests a strike where the OI significantly exceeds a configured threshold.

### 3. 💥 Exhaustion Reversal (Medium Priority)
*   **Logic:** Catches "blow-off tops" or "panic bottoms" using volume/price divergence.
*   **Dynamic Targets:** Uses structural levels to define exit zones, ensuring realistic profit booking.
*   **Triggers:** Triggers when a volume climax (extreme spike) coincides with a doji-like indecision candle at a local price extreme, often accompanied by an IV spike.

---

## 🛡️ Trade Management (Position Manager)
ARES actively tracks its signals using a persistent **Position Manager**:
*   **Deterministic Targets**: The system ensures that **Target 1 (T1)** is always the level closest to the entry price. Additionally, any structural level within **20 points** of the entry is filtered out to ensure targets remain significant.
*   **Trailing Stops**: Once a trade reaches Target 1 (T1), the Stop Loss is automatically trailed to the entry price to lock in a risk-free position.
*   **Persistent State**: Active trades are synced in real-time with a Supabase PostgreSQL database (`active_trades` table) and loaded into memory on startup, ensuring no data loss across system restarts. Connection is resilient with lazy initialization.
*   **Tracking IDs**: Every generated trade signal is assigned a random 4-digit identifier (e.g., `#0501`) that persists through all subsequent trade updates.
*   **Analytics Persistence**: Detailed trade histories, market context, and OI data are logged to a `trade_analytics` table upon trade completion for rigorous monthly performance analysis and ML training.
*   **Discord Tracking**: Any state change (hitting T1 or Stop Loss) instantly triggers a dedicated Discord update via Webhooks with color-coded formatting.

---

## 📊 Backtesting & Visualization
ARES includes a robust backtesting module to validate strategies against historical data:
*   **High-Fidelity Simulation**: Simulates trade execution using historical 1-minute OHLC data.
*   **PineScript Exporter**: Generates TradingView-compatible PineScript (v6) code. This allows traders to visually inspect every signal, entry, stop-loss, and target level directly on a TradingView chart.
*   **Performance Metrics**: Automatically calculates PnL, Win Rate, and Drawdown for the simulated period.

---

## 🛠️ Tech Stack & Engineering Paradigms

*   **Language:** Python 3.10+ (Asyncio-driven for non-blocking I/O).
*   **HTTP Client:** `httpx` for high-performance asynchronous API requests.
*   **Data Models:** Heavy use of **Python Dataclasses** for O(1) state management and memory efficiency.
*   **Configuration:** **Pydantic-Settings** for a "fail-fast" paradigm—invalid environment variables prevent the system from starting.
*   **Broker API:** [DhanHQ Python SDK](https://dhanhq.co/docs/v2/) (Primary data ingress).
*   **Database:** Supabase (PostgreSQL) for persistence.

---

## 📂 Project Structure

```text
ares/
├── config.py          # Strict Pydantic configuration & thresholds
├── config_profiles.py # Performance configurations & thresholds
├── models.py          # Domain models (OHLCVCandle, AresSignal, OptionRow)
├── fetchers/          # Ingestion Layer
│   ├── price_fetcher.py   # DhanHQ minute data, VWAP logic & dynamic PDH/PDL via Dhan Historical Daily API
│   ├── oi_fetcher.py      # DhanHQ Option Chain processing
│   └── level_fetcher.py   # Dynamic level construction (PDH/PDL + OI walls)
├── detectors/         # Detection Layer
│   ├── breakout.py        # Stateful detector for Failed Breakouts
│   ├── oi_wall.py         # Stateless structural rejection detector
│   ├── exhaustion.py      # Volume history & price extreme tracker
│   └── expiry_detector.py # Expiry day detection (Dhan API check/Tuesday fallback)
├── backtest/          # Analysis Layer
│   ├── pinescript_exporter.py # Generates TradingView v6 visualization scripts
│   ├── engine.py          # Historical simulation engine
│   └── report.py          # Performance metrics & PDF/Markdown reporting
├── engine.py          # The "Brain" — Orchestrates evaluation & priority
├── position_manager.py # Persistent trade position & trailing stop manager
├── alerts.py          # Formatting & Discord broadcasting
├── storage.py         # Supabase persistence logic
├── main.py            # Entry point, polling loop, and session gate
```

---

## ⚙️ Setup & Deployment

### 1. Environment Configuration
Clone the repository and create a `.env` file based on `.env.example`.

```bash
# ==========================================
# Dhan API
# ==========================================
DHAN_CLIENT_ID="your_client_id"
DHAN_ACCESS_TOKEN="your_jwt_token"
NIFTY_SECURITY_ID="13"           # Default for NIFTY 50
NIFTY_EXCHANGE="IDX_I"           # Default for Indices

# ==========================================
# Discord
# ==========================================
DISCORD_WEBHOOK_URL="your_discord_webhook"
DISCORD_HEALTH_WEBHOOK_URL="your_health_webhook"  # Optional: For heartbeats and system alerts

# ==========================================
# Supabase
# ==========================================
SUPABASE_URL="your_supabase_url"
SUPABASE_KEY="your_supabase_anon_key"

# ==========================================
# Engine Parameters
# ==========================================
POLL_INTERVAL_SECONDS=60
SIGNAL_COOLDOWN_MINUTES=5
CANDLE_BUFFER_SIZE=30
IV_BUFFER_SIZE=10

# ==========================================
# Detector Configuration
# ==========================================
BREAKOUT_CONFIRMATION_CANDLES=3
BREAKOUT_FAILURE_MIN_SCORE=2
BREAKOUT_WEAK_VOLUME_RATIO=0.8
BREAKOUT_IV_FALLING_THRESHOLD=-3.0
BREAKOUT_STOP_BUFFER=30.0
BREAKOUT_RESISTANCE_PROXIMITY=20.0

OI_WALL_MIN_OI=5000000
OI_WALL_MIN_OI_CHANGE_PCT=20.0
OI_WALL_APPROACH_DISTANCE=40.0
OI_WALL_TEST_DISTANCE=20.0

EXHAUSTION_VOLUME_MULTIPLIER=2.5
EXHAUSTION_BODY_RATIO=0.3
EXHAUSTION_IV_SPIKE_THRESHOLD=1.0
EXHAUSTION_MIN_CANDLES=5

# ==========================================
# Targets & Zones
# ==========================================
TARGET_1_PTS=40.0
TARGET_2_PTS=80.0
STRIKE_INTERVAL=50
ENTRY_ZONE_OFFSET_PTS=5.0
OI_WALL_STOP_BUFFER=10.0
EXHAUSTION_STOP_BUFFER=20.0
LEVEL_SCAN_RANGE=500.0
```

### 2. Database Initialization
Run the following schema in your Supabase SQL Editor to prepare the persistence layer:

```sql
CREATE TABLE ares_signals (
  id bigserial primary key,
  setup_type text,
  direction text,
  confidence text,
  trigger_price numeric,
  spot_at_signal numeric,
  stop_loss numeric,
  target_1 numeric,
  target_2 numeric,
  strike integer,
  option_type text,
  reasons jsonb,
  timestamp timestamptz,
  created_at timestamptz default now()
);

-- Optional: Add an index on timestamp for faster time-series queries later
CREATE INDEX idx_ares_signals_timestamp ON ares_signals (timestamp DESC);

CREATE TABLE active_trades (
  id uuid primary key,
  signal_id text,
  setup_type text not null,
  direction text not null,
  entry_price numeric not null,
  stop_loss numeric not null,
  target_1 numeric not null,
  target_2 numeric not null,
  state text not null default 'OPEN',
  added_time_ist text,
  created_at timestamptz default now()
);

CREATE TABLE trade_analytics (
  id uuid PRIMARY KEY,
  signal_id bigint, -- Optional link to ares_signals
  setup_type text NOT NULL,
  direction text NOT NULL,
  
  -- Price & Time
  entry_timestamp timestamptz NOT NULL,
  exit_timestamp timestamptz,
  entry_price numeric NOT NULL,
  exit_price numeric,
  pnl_points numeric,
  
  -- Outcome
  result_state text DEFAULT 'OPEN', -- OPEN, T1_HIT, T2_HIT, STOPPED_OUT, EXPIRED
  
  -- Deep Context (JSONB for ML flexibility)
  market_context jsonb, -- { "reasons": [...], "spot_at_signal": 24500, "confidence": "HIGH" }
  oi_data jsonb,        -- { "pcr": 0.8, "atm_ce_oi": 1200000, "atm_pe_oi": 1500000, "oi_change_pct": 5.2 }
  
  created_at timestamptz DEFAULT now()
);

-- Index for temporal analysis
CREATE INDEX idx_trade_analytics_entry ON trade_analytics (entry_timestamp DESC);
```

### 3. Row Level Security (RLS) Note
If you use the **Anon/Public key** (default in `.env`), you must either disable RLS or add a permissive policy for the bot to work. If you see code `42501`, run these in the SQL Editor:

```sql
ALTER TABLE active_trades DISABLE ROW LEVEL SECURITY;
ALTER TABLE ares_signals DISABLE ROW LEVEL SECURITY;
ALTER TABLE trade_analytics DISABLE ROW LEVEL SECURITY;
```

### 4. Local Execution
Start the monitoring engine:

```bash
python main.py
```

### 4. Fly.io Deployment
ARES is fully dockerized and configured for Fly.io.

1. Install `flyctl` and login.
2. Run `fly launch` (do not override the existing `fly.toml` unless needed).
3. Set your secrets in Fly:
```bash
fly secrets set DHAN_CLIENT_ID="your_id" DHAN_ACCESS_TOKEN="your_token" SUPABASE_URL="your_url" SUPABASE_KEY="your_key" DISCORD_WEBHOOK_URL="your_webhook"
```
4. Deploy the application:
```bash
fly deploy
```

The application is configured to automatically scale up at 09:15 IST and down at 15:25 IST via a GitHub Actions cron job.

---

## 🚀 Operational Parameters

*   **Session Window:** Actively polls from **09:20 to 15:25 IST** (Standard NSE session).
*   **Warmup State:** Requires **30 candles** (configurable via `CANDLE_BUFFER_SIZE`) to fill rolling volume/IV buffers before generating signals.
*   **Premium UI:** Full ANSI color support in the terminal for high-visibility signal monitoring.
*   **Signal Cooldown:** Enforces a strict **5-minute cooldown** between alerts to prevent over-trading in choppy conditions.
*   **Zero Hardcoding:** All thresholds (Volume ratios, IV drops, OI walls) are fully configurable via environment variables.

---

## ⚠️ Disclaimer

ARES is an algorithmic signaling tool designed for educational and informational purposes. It does **not** execute trades automatically. Options trading carries significant risk. Always forward-test in a paper-trading environment before risking real capital.
