# Product Manager (PM) Directive: ARES Trading System

## 1. Project Overview
**ARES** (Adaptive Reversal & Entry Signal) is an automated signal generation engine designed specifically for **NIFTY 50 Options Scalping**. The system targets 1-minute reversals and continuation setups based on price-volume dynamics, Option Interest (OI) walls, and Implied Volatility (IV) changes.

## 2. Target Audience
- Professional Options Scalpers.
- Quantitative traders requiring real-time structural data (OI Walls, VWAP).
- Technical developers interested in modular trading system architecture.

## 3. Core Functional Requirements
- **Real-time Monitoring:** Asynchronous polling of 1-minute candles and Option Chain data from DhanHQ.
- **Structural Analysis:** Automated detection of Previous Day High (PDH) and Previous Day Low (PDL) along with real-time "OI Walls" (strikes with massive writer accumulation).
- **Signal Logic:**
    - **Failed Breakout:** Detect when price crosses a level but fails to sustain, scored against volume and OI defense.
    - **OI Wall Rejection:** Identify structural bounces from high-OI strikes.
    - **Exhaustion Reversal:** Spot volume climaxes at price extremes.
- **Alerting:** Real-time delivery of actionable signals via Discord and Console.
- **Persistence:** Reliable logging of every signal for performance review and historical analysis.

## 4. Operational Constraints
- **Session Control:** The system must only operate during active NSE session hours (09:20 - 15:25 IST).
- **Spam Mitigation:** A minimum 5-minute cooldown between signals is required.
- **Data Integrity:** The system must handle API authentication errors and connectivity drops gracefully without crashing.

## 5. Success Metrics
- **Latency:** Signal generation within < 2 seconds of candle close.
- **Reliability:** 100% uptime during session hours.
- **Persistence:** Accurate logging of every generated signal to Supabase.
