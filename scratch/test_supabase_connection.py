"""
Quick Supabase connectivity check.
Run: python scratch/test_supabase_connection.py
"""
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from supabase import create_client

def main():
    print(f"Supabase URL : {settings.supabase_url[:30]}...")
    print(f"Supabase Key : {settings.supabase_key[:10]}...")
    print()

    try:
        client = create_client(settings.supabase_url, settings.supabase_key)
        print("✅ Client created successfully.")
    except Exception as e:
        print(f"❌ Client creation failed: {e}")
        return

    # Test 1: Read from ares_signals
    try:
        res = client.table("ares_signals").select("*").limit(1).execute()
        print(f"✅ ares_signals  — connected (rows found: {len(res.data)})")
    except Exception as e:
        print(f"❌ ares_signals  — {e}")

    # Test 2: Read from active_trades
    try:
        res = client.table("active_trades").select("*").limit(1).execute()
        print(f"✅ active_trades — connected (rows found: {len(res.data)})")
    except Exception as e:
        print(f"❌ active_trades — {e}")

    print("\n🎯 Supabase connection is working!" )

if __name__ == "__main__":
    main()
