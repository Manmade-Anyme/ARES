import asyncio
import httpx
from datetime import datetime, timezone, timedelta

async def main():
    url = "https://query2.finance.yahoo.com/v8/finance/chart/^NSEI?range=5d&interval=1d"
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        data = resp.json()
        result = data['chart']['result'][0]
        timestamps = result['timestamp']
        
        now_ist_date = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
        print(f"Now IST: {now_ist_date}")

        for t in reversed(timestamps):
            dt_ist = (datetime.fromtimestamp(t, tz=timezone.utc) + timedelta(hours=5, minutes=30)).date()
            print(f"Timestamp {t} -> dt_ist: {dt_ist}, dt_ist < now_ist_date: {dt_ist < now_ist_date}")

asyncio.run(main())
