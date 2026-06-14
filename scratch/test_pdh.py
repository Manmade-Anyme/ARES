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
        highs = result['indicators']['quote'][0]['high']
        lows = result['indicators']['quote'][0]['low']
        closes = result['indicators']['quote'][0]['close']
        
        print("Data from Yahoo:")
        for t, h, l, c in zip(timestamps, highs, lows, closes):
            if h is not None:
                dt = datetime.fromtimestamp(t, tz=timezone.utc)
                dt_ist = dt + timedelta(hours=5, minutes=30)
                print(f"{dt_ist.date()} - High: {h:.2f}, Low: {l:.2f}, Close: {c:.2f}")

asyncio.run(main())
