-- MANM-151: persist terminal state needed to recover raced analytics exits.
ALTER TABLE active_trades
  ADD COLUMN IF NOT EXISTS exit_price numeric,
  ADD COLUMN IF NOT EXISTS exit_type text,
  ADD COLUMN IF NOT EXISTS exit_timestamp timestamptz,
  ADD COLUMN IF NOT EXISTS pnl_points_override numeric;
