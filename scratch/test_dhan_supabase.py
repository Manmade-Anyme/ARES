import asyncio
import os
import sys

# Add parent directory to path so we can import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from storage import load_dhan_credentials_from_supabase
from dhanhq import dhanhq, DhanContext

async def test():
    # 1. Fetch credentials from Supabase and update settings
    load_dhan_credentials_from_supabase()
    
    print(f"Loaded Client ID: {settings.dhan_client_id}")
    print(f"Loaded Access Token (first 10 chars): {settings.dhan_access_token[:10]}...")
    
    # 2. Reinitialize Context with updated settings
    context = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
    dhan = dhanhq(context)
    
    # 3. Test expiry list retrieval
    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        None,
        lambda: dhan.expiry_list(
            under_security_id=int(settings.security_id),
            under_exchange_segment=settings.exchange_segment
        )
    )
    
    print("Dhan API Expiry List Response Status:", response.get("status"))
    if response.get("status") == "success":
        print("✅ SUCCESS: Successfully authenticated with Dhan API using Supabase credentials!")
        print("Data sample:", response.get("data", {}).get("data", [])[:2])
    else:
        print("❌ FAIL: Authentication failed or bad response from Dhan API:", response)

if __name__ == "__main__":
    asyncio.run(test())
