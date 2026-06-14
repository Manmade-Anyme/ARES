import asyncio
from config import settings
from alerts import send_startup_alert, send_error_alert
from fetchers.price_fetcher import PriceFetcher

async def test():
    print("[*] Fetching dynamic PDH and PDL for the test...")
    pf = PriceFetcher()
    try:
        pdh, pdl = await pf.fetch_previous_day_ohlc()
        print(f"[+] Fetched PDH/PDL: {pdh:.2f} / {pdl:.2f}")
    except Exception as e:
        print(f"[-] Failed to fetch dynamic PDH/PDL: {e}")
        pdh, pdl = 24100.0, 23900.0

    # Test Signal Webhook via Startup Alert
    print(f"\n[*] Testing Signal/Startup Webhook...")
    try:
        await send_startup_alert(pdh, pdl)
        print("[+] Signal Webhook Success (Startup Alert)")
    except Exception as e:
        print(f"[-] Signal Webhook Failed: {e}")

    # Test Health Webhook via Error Alert
    print(f"\n[*] Testing Health Webhook...")
    try:
        await send_error_alert("Test error message")
        print("[+] Health Webhook Success (Error Alert)")
    except Exception as e:
        print(f"[-] Health Webhook Failed: {e}")

asyncio.run(test())
