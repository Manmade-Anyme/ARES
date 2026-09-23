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

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

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
  oi_wall_context jsonb, -- TASK-073: decoupled entry telemetry (NULL for non-OI-wall)
  signal_uuid uuid UNIQUE,
  display_id text,
  timestamp timestamptz,
  created_at timestamptz default now()
);

-- Optional: Add an index on timestamp for faster time-series queries later
CREATE INDEX idx_ares_signals_timestamp ON ares_signals (timestamp DESC);

CREATE TABLE active_trades (
  id uuid primary key,
  signal_id text,
  signal_uuid uuid CONSTRAINT fk_active_trades_signal_uuid REFERENCES ares_signals(signal_uuid),
  display_id text,
  setup_type text not null,
  direction text not null,
  entry_timestamp timestamptz not null,
  entry_price numeric not null,
  stop_loss numeric not null,
  target_1 numeric not null,
  target_2 numeric not null,
  state text not null default 'OPEN',
  exit_price numeric,
  exit_type text,
  exit_timestamp timestamptz,
  pnl_points_override numeric,
  added_time_ist text,
  time_metrics_excluded boolean NOT NULL DEFAULT false,
  created_at timestamptz default now(),
  CONSTRAINT chk_active_trades_exit_chronology CHECK (time_metrics_excluded OR exit_timestamp IS NULL OR exit_timestamp >= entry_timestamp),
  CONSTRAINT chk_active_trades_closed_requires_exit CHECK (state NOT IN ('CLOSED', 'STOPPED_OUT') OR exit_timestamp IS NOT NULL OR time_metrics_excluded = true)
);

-- Note: If you encounter RLS errors (Code 42501), run these in the Supabase SQL Editor:
-- ALTER TABLE active_trades DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE ares_signals DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE trade_analytics DISABLE ROW LEVEL SECURITY;

CREATE TABLE trade_analytics (
  id uuid PRIMARY KEY,
  signal_id text, -- Optional link to ares_signals
  signal_uuid uuid CONSTRAINT fk_trade_analytics_signal_uuid REFERENCES ares_signals(signal_uuid),
  setup_type text NOT NULL,
  direction text NOT NULL,
  
  -- Price & Time
  entry_timestamp timestamptz NOT NULL,
  exit_timestamp timestamptz,
  exit_signal_uuid uuid UNIQUE,
  display_id text,
  timestamp timestamptz,
  entry_price numeric NOT NULL,
  exit_price numeric,
  pnl_points numeric,
  score integer,
  
  -- Outcome
  result_state text DEFAULT 'OPEN', -- OPEN, T1_HIT, T2_HIT, STOPPED_OUT, EXPIRED
  
  -- Deep Context (JSONB for ML flexibility)
  market_context jsonb, -- { "reasons": [...], "spot_at_signal": 24500, "confidence": "HIGH", "options_sizing": { "suggested_lots": 1, "option_sl": 70.0, "option_target": 115.0, ... } }
  oi_data jsonb,        -- { "pcr": 0.8, "atm_ce_oi": 1200000, "atm_pe_oi": 1500000, "oi_change_pct": 5.2 }
  
  time_metrics_excluded boolean NOT NULL DEFAULT false,
  created_at timestamptz DEFAULT now(),
  CONSTRAINT chk_trade_analytics_exit_chronology CHECK (time_metrics_excluded OR exit_timestamp IS NULL OR exit_timestamp >= entry_timestamp),
  CONSTRAINT chk_trade_analytics_closed_requires_exit CHECK (result_state = 'OPEN' OR exit_timestamp IS NOT NULL OR time_metrics_excluded = true)
);

-- Index for temporal analysis
CREATE INDEX idx_trade_analytics_entry ON trade_analytics (entry_timestamp DESC);

-- Atomic trade entry RPC for bridge mode
CREATE OR REPLACE FUNCTION create_trade_entry_bridge(
  p_trade_id uuid,
  p_signal_uuid uuid,
  p_display_id text,
  p_setup_type text,
  p_direction text,
  p_entry_price numeric,
  p_stop_loss numeric,
  p_target_1 numeric,
  p_target_2 numeric,
  p_added_time_ist text,
  p_market_context jsonb,
  p_oi_data jsonb,
  p_entry_timestamp timestamptz,
  p_legacy_signal_id bigint
) RETURNS uuid
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO active_trades (
    id, signal_id, signal_uuid, display_id, setup_type, direction, entry_timestamp, entry_price,
    stop_loss, target_1, target_2, state, added_time_ist
  ) VALUES (
    p_trade_id, p_legacy_signal_id::text, p_signal_uuid, p_display_id,
    p_setup_type, p_direction, p_entry_timestamp, p_entry_price,
    p_stop_loss, p_target_1, p_target_2, 'OPEN', p_added_time_ist
  ) ON CONFLICT (id) DO NOTHING;

  INSERT INTO trade_analytics (
    id, signal_id, signal_uuid, setup_type, direction, entry_price,
    market_context, oi_data, entry_timestamp, result_state
  ) VALUES (
    p_trade_id, p_legacy_signal_id, p_signal_uuid, p_setup_type, p_direction, p_entry_price,
    p_market_context, p_oi_data, p_entry_timestamp, 'OPEN'
  ) ON CONFLICT (id) DO NOTHING;

  IF NOT EXISTS (
    SELECT 1 FROM active_trades
    WHERE id = p_trade_id
      AND signal_id IS NOT DISTINCT FROM p_legacy_signal_id::text
      AND signal_uuid IS NOT DISTINCT FROM p_signal_uuid
      AND display_id IS NOT DISTINCT FROM p_display_id
      AND setup_type IS NOT DISTINCT FROM p_setup_type
      AND direction IS NOT DISTINCT FROM p_direction
      AND entry_timestamp IS NOT DISTINCT FROM p_entry_timestamp
      AND entry_price IS NOT DISTINCT FROM p_entry_price
      AND stop_loss IS NOT DISTINCT FROM p_stop_loss
      AND target_1 IS NOT DISTINCT FROM p_target_1
      AND target_2 IS NOT DISTINCT FROM p_target_2
  ) OR NOT EXISTS (
    SELECT 1 FROM trade_analytics
    WHERE id = p_trade_id
      AND signal_id IS NOT DISTINCT FROM p_legacy_signal_id::text
      AND signal_uuid IS NOT DISTINCT FROM p_signal_uuid
      AND setup_type IS NOT DISTINCT FROM p_setup_type
      AND direction IS NOT DISTINCT FROM p_direction
      AND entry_price IS NOT DISTINCT FROM p_entry_price
      AND market_context IS NOT DISTINCT FROM p_market_context
      AND oi_data IS NOT DISTINCT FROM p_oi_data
      AND entry_timestamp IS NOT DISTINCT FROM p_entry_timestamp
  ) THEN
    RAISE EXCEPTION 'Conflicting trade entry for id %', p_trade_id;
  END IF;

  RETURN p_trade_id;
END;
$$;
