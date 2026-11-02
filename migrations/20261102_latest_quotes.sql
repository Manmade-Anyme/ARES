-- Migration: 20261102_latest_quotes.sql
-- Purpose: Persist latest market quotes (underlying spot and selected option current LTP)
--          keyed by instrument to avoid per-tick inserts while enabling dashboard live quotes.
-- Note: Do NOT apply directly to production during task development.

CREATE TABLE IF NOT EXISTS public.latest_quotes (
    instrument_id TEXT PRIMARY KEY,
    instrument_type TEXT NOT NULL CHECK (instrument_type IN ('UNDERLYING', 'OPTION')),
    symbol TEXT NOT NULL DEFAULT 'NIFTY',
    price NUMERIC NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    expiry TEXT,
    strike NUMERIC,
    option_type TEXT CHECK (option_type IS NULL OR option_type IN ('CE', 'PE')),
    security_id TEXT,
    raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index on instrument_type and updated_at for fast retrieval
CREATE INDEX IF NOT EXISTS idx_latest_quotes_type_updated
    ON public.latest_quotes (instrument_type, updated_at DESC);

-- Enable Row Level Security
ALTER TABLE public.latest_quotes ENABLE ROW LEVEL SECURITY;

-- Allow read-only access to anon and authenticated clients (for dashboard/SpanQuant frontend/server API)
CREATE POLICY "Allow public read access to latest quotes"
    ON public.latest_quotes
    FOR SELECT
    TO anon, authenticated
    USING (true);

-- Restrict mutations (insert, update, delete) to service_role only
CREATE POLICY "Allow service_role full access to latest quotes"
    ON public.latest_quotes
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- RPC helper to fetch latest quotes formatted for SpanQuant frontend contract
CREATE OR REPLACE FUNCTION public.get_latest_market_quotes()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE
    underlying_row RECORD;
    option_row RECORD;
    result jsonb;
BEGIN
    SELECT * INTO underlying_row
    FROM public.latest_quotes
    WHERE instrument_id = 'UNDERLYING:NIFTY'
    LIMIT 1;

    SELECT * INTO option_row
    FROM public.latest_quotes
    WHERE instrument_id = 'OPTION:SELECTED'
    LIMIT 1;

    result := jsonb_build_object(
        'underlying', CASE
            WHEN underlying_row.price IS NOT NULL THEN
                jsonb_build_object(
                    'symbol', underlying_row.symbol,
                    'currentSpot', underlying_row.price,
                    'updatedAt', to_char(underlying_row.observed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                    'source', underlying_row.source
                )
            ELSE NULL
        END,
        'selectedOption', CASE
            WHEN option_row.price IS NOT NULL AND option_row.security_id IS NOT NULL THEN
                jsonb_build_object(
                    'expiry', option_row.expiry,
                    'strike', option_row.strike,
                    'optionType', option_row.option_type,
                    'securityId', option_row.security_id,
                    'currentPremium', option_row.price,
                    'updatedAt', to_char(option_row.observed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                    'source', option_row.source
                )
            ELSE NULL
        END
    );

    RETURN result;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_latest_market_quotes() TO anon, authenticated, service_role;
