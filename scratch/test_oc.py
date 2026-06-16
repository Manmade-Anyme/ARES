import asyncio
from config import settings
from dhanhq import dhanhq, DhanContext

def test_oc():
    context = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
    dhan = dhanhq(context)
    try:
        res = dhan.expiry_list(under_security_id=int(settings.security_id), under_exchange_segment=settings.exchange_segment)
        print("Expiry res:", res)
        if res and res.get("data", {}).get("data"):
            nearest = res["data"]["data"][0]
            print("Nearest expiry:", nearest)
            oc_res = dhan.option_chain(under_security_id=int(settings.security_id), under_exchange_segment=settings.exchange_segment, expiry=nearest)
            print("OC res:", oc_res)
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    test_oc()
