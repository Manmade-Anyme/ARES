import os
import sys
import uuid
from datetime import datetime, timezone

# Add parent directory to path so we can import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client, Client
from config import settings

def test_supabase_schema():
    print("Connecting to Supabase...")
    supabase: Client = create_client(settings.supabase_url, settings.supabase_key)
    
    trade_id = str(uuid.uuid4())
    test_data = {
        "id": trade_id,
        "signal_id": "TEST_9999",
        "setup_type": "TEST",
        "direction": "BULLISH",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "state": "CLOSED",  # closed so it won't be picked up by the position manager
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    try:
        print(f"Attempting to insert test record with signal_id 'TEST_9999'...")
        response = supabase.table("active_trades").insert(test_data).execute()
        print("✅ SUCCESS: Schema matches! Inserted record with signal_id.")
        
        # Verify it can be read back
        read_response = supabase.table("active_trades").select("signal_id").eq("id", trade_id).execute()
        if read_response.data and read_response.data[0].get("signal_id") == "TEST_9999":
            print("✅ SUCCESS: Successfully read signal_id back from database.")
        else:
            print("❌ FAIL: Could not read signal_id back.")
            
        # Clean up the test record
        print("Cleaning up test record...")
        supabase.table("active_trades").delete().eq("id", trade_id).execute()
        print("✅ SUCCESS: Test record removed.")
        
    except Exception as e:
        print(f"❌ FAIL: {e}")

if __name__ == "__main__":
    test_supabase_schema()
