import asyncio
import httpx
from config import settings

async def test():
    url = settings.discord_webhook_url
    msg = f"""```diff
+ =================================================================
+ 🤖 ARES (Adaptive Reversal & Entry Signal) - Initialization
+ =================================================================
+ [+] Target Asset : ^NSEI (1-minute timeframe)
+ [+] Detectors    : Failed Breakout, OI Wall, Exhaustion
+ [+] Session      : 09:15 to 23:30 IST
+ [+] Cooldown     : 5 minutes between signals
+ [+] PDH / PDL    : 24334.69921875 / 24059.94921875
+ =================================================================
+ ```"""

    payload = {"content": msg}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            print("Success")
        except Exception as e:
            print(f"Exception type: {type(e)}")
            print(f"Exception message: {e}")
            if hasattr(e, 'response') and e.response:
                print(f"Response text: {e.response.text}")

asyncio.run(test())
