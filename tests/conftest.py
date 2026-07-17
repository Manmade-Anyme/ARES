"""Pytest session setup.

Unit tests must never reach the live Supabase project.

This file used to call load_dhan_credentials_from_supabase() at session start.
That did two harmful things (TASK-188):

  1. It required network access and live credentials to run unit tests.
  2. It imported `storage` unmocked before tests/unit/test_storage.py could
     install its `supabase.create_client` patch. That patch is import-order
     dependent, so when it lost the race Storage() bound a REAL client and
     test_storage.py's log_signal() fixtures were written straight into the
     production tables — ares_signals ids 167-170 (spot=24001.0,
     trigger=24000.0, reason "Reason 1") plus nine trade_analytics rows. They
     were indistinguishable from real signals in the OI-wall P&L history and
     inflated its measured average from +3.76 to +14.34 pts/trade.

Every test that needs Dhan or Supabase values mocks them, so the safe default
is to point the credentials at unroutable dummies. If a real client is ever
constructed by accident it now fails loudly instead of writing to production.
"""
from config import settings

# Unroutable by design — a client built with these cannot reach production.
_DUMMY_SUPABASE_URL = "http://supabase.invalid"
_DUMMY_SUPABASE_KEY = "test-key-never-a-real-credential"


def pytest_sessionstart(session):
    settings.supabase_url = _DUMMY_SUPABASE_URL
    settings.supabase_key = _DUMMY_SUPABASE_KEY
    settings.dhan_client_id = "test-client-id"
    settings.dhan_access_token = "test-access-token"
