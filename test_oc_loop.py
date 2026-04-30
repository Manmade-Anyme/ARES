import asyncio
import time
from config import settings
from dhanhq import dhanhq, DhanContext

def test_oc():
    context = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
    dhan = dhanhq(context)
    res = dhan.expiry_list(under_security_id=int(settings.security_id), under_exchange_segment=settings.exchange_segment)
    nearest = res["data"]["data"][0]
    for i in range(5):
        oc_res = dhan.option_chain(under_security_id=int(settings.security_id), under_exchange_segment=settings.exchange_segment, expiry=nearest)
        data = oc_res.get("data")
        if isinstance(data, dict):
            print(f"Cycle {i} SUCCEEDED. (status: {oc_res.get('status')})")
        else:
            print(f"Cycle {i} FAILED. data: {repr(data)} full: {oc_res}")
        time.sleep(1)

if __name__ == "__main__":
    test_oc()
