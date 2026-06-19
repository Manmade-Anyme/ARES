import pytest
from storage import load_dhan_credentials_from_supabase

def pytest_sessionstart(session):
    """
    Load Dhan credentials dynamically from Supabase before running tests.
    """
    try:
        load_dhan_credentials_from_supabase()
    except Exception as e:
        print(f"WARNING: Could not load Dhan credentials for tests: {e}")
