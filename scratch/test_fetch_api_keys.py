import os
import sys

# Add parent directory to path so we can import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client, Client
from config import settings

def test_fetch():
    print("Connecting to Supabase at", settings.supabase_url)
    supabase: Client = create_client(settings.supabase_url, settings.supabase_key)
    try:
        response = supabase.table("api_keys").select("*").execute()
        print("Data in api_keys:")
        print(response.data)
    except Exception as e:
        print("Error fetching api_keys:", e)

if __name__ == "__main__":
    test_fetch()
