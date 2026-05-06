import asyncio
from dhanhq import dhanhq
from config import settings
from datetime import datetime

async def test_dhan():
    try:
        from dhanhq import DhanContext
        context = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
        dhan = dhanhq(context)
    except ImportError:
        dhan = dhanhq(settings.dhan_client_id, settings.dhan_access_token)
    
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"Testing intraday_minute_data for {today}")
    print(f"Params: security_id={settings.security_id}, segment={settings.exchange_segment}, type={settings.instrument_type}")
    
    response = dhan.intraday_minute_data(
        settings.security_id,
        settings.exchange_segment,
        settings.instrument_type,
        today,
        today
    )
    print(f"Response: {response}")

if __name__ == "__main__":
    asyncio.run(test_dhan())
