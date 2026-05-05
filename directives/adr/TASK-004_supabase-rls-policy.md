# TASK-004: Supabase Row Level Security (RLS) Configuration
**Date:** 2026-05-05
**Status:** accepted

## Problem Statement
The ARES system uses Supabase (PostgreSQL) for persisting signals and active trade states. When deploying with the default "anon" (publishable) key, write operations (`INSERT`, `UPDATE`, `DELETE`) fail with error code `42501` (Row Level Security policy violation) because Supabase enables RLS by default on new tables but does not provide default policies for anonymous access.

## Decision
We will explicitly document and implement a policy to disable RLS for the `active_trades` and `ares_signals` tables. 

While keeping RLS enabled and defining specific policies for the `anon` role is more "secure," it adds unnecessary configuration complexity for a personal trading bot where the database is primarily a local state persistence mechanism. Disabling RLS for these specific tables is the most robust and user-friendly path for the current MVP.

## Alternatives Considered
1. **Enable RLS with "Allow All" Policy**:
   ```sql
   CREATE POLICY "Allow all" ON active_trades FOR ALL USING (true) WITH CHECK (true);
   ```
   *Rationale for Rejection*: Slightly more verbose than simply disabling RLS and achieves the same outcome for this specific use case.
2. **Switch to Service Role Key**:
   *Rationale for Rejection*: The Service Role Key bypasses RLS entirely but is highly sensitive. We prefer to keep the default `.env` configuration using the `anon` key to minimize the risk of accidental key exposure during development/debugging.

## Implementation Details
The following commands must be executed in the Supabase SQL Editor:
```sql
ALTER TABLE active_trades DISABLE ROW LEVEL SECURITY;
ALTER TABLE ares_signals DISABLE ROW LEVEL SECURITY;
```

These instructions have been added to:
- `README.md` (Setup instructions)
- `schema.sql` (Inline troubleshooting notes)
- `CHANGELOG.md` (Fix record)

## Security Considerations
Since the `anon` key is publishable, anyone with the key and the project URL can read/write to these tables. For a private trading bot, this is acceptable. If the system is ever expanded to a multi-user platform, RLS must be re-enabled with proper `auth.uid()` checks.

## Definition of Done
- [x] Diagnostic script confirms `INSERT` and `SELECT` work without 42501 errors.
- [x] Documentation updated in `README.md` and `schema.sql`.
- [x] ADR recorded.
