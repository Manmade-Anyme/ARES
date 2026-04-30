import asyncio
import httpx

async def test():
    url = "https://discord.com/api/webhooks/1490772670910824618/tWEUD6hre51E1m8-zDw8ffgf0t6yQ2ga_vmA3iCOXZnf69W_BmvDGQ-xaAMmHeEG-_2c"
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
