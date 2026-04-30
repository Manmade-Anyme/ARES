import asyncio
from config import settings
from dhanhq import dhanhq, DhanContext
from datetime import datetime

async def test():
    context = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
    dhan = dhanhq(context)
    today = datetime.now().strftime("%Y-%m-%d")
    res = dhan.intraday_minute_data(
        settings.security_id,
        settings.exchange_segment,
        settings.instrument_type,
        today,
        today
    )
    print(type(res))
    if isinstance(res, dict):
        print(res.keys())
        if 'data' in res:
            print("data type:", type(res['data']))

asyncio.run(test())
