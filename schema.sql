-- ARES Supabase Schema
-- Run this in the Supabase SQL Editor

-- Render timestamps as IST on read (TASK-189). Every timestamp column below is
-- timestamptz, which stores an absolute UTC instant regardless — this only
-- affects how sessions display them, and takes effect on new connections.
--
-- Scoped to the role deliberately, NOT `ALTER DATABASE`: the live Supabase
-- project is shared with Gamma Blaster (gb_*), Kronos, Phantom, Sniper and
-- Order Flow. A database-wide setting would change what PostgREST renders for
-- all of them. This affects SQL Editor / psql sessions only; app connections
-- (PostgREST authenticates as `authenticator`) are untouched.
ALTER ROLE postgres SET timezone TO 'Asia/Kolkata';

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

-- Note: If you encounter RLS errors (Code 42501), run these in the Supabase SQL Editor:
-- ALTER TABLE active_trades DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE ares_signals DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE trade_analytics DISABLE ROW LEVEL SECURITY;

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
  market_context jsonb, -- { "reasons": [...], "spot_at_signal": 24500, "confidence": "HIGH", "options_sizing": { "suggested_lots": 1, "option_sl": 70.0, "option_target": 115.0, ... } }
  oi_data jsonb,        -- { "pcr": 0.8, "atm_ce_oi": 1200000, "atm_pe_oi": 1500000, "oi_change_pct": 5.2 }
  
  created_at timestamptz DEFAULT now()
);

-- Index for temporal analysis
CREATE INDEX idx_trade_analytics_entry ON trade_analytics (entry_timestamp DESC);
