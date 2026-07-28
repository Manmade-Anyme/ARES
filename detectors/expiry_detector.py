"""
ARES Expiry Day Detection

Determines whether today is a Nifty weekly expiry day by fetching the 
expiry list from the Dhan API and comparing against today's date (IST).

Fallback: If the API call fails (e.g., before market hours or on weekends),
defaults to Tuesday as the standard weekly expiry day.
"""

import asyncio
from datetime import datetime, date, timezone, timedelta
from typing import Optional

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    """Get today's date in IST."""
    return datetime.now(IST).date()


def is_expiry_day_simple() -> bool:
    """
    Simple fallback: Tuesday (weekday=1) is expiry day.
    Used when the API-based check isn't available.
    """
    return _today_ist().weekday() == 1  # Monday=0, Tuesday=1


def days_to_expiry(expiry: str) -> Optional[int]:
    """
    Calendar days from today (IST) to the given expiry date.

    Args:
        expiry: Expiry date as "YYYY-MM-DD" (the shape OIFetcher.get_nearest_expiry
            returns). Some Dhan responses carry a trailing time component, so only
            the date part is parsed.

    Returns:
        Days remaining — 0 on expiry day itself — or None if `expiry` is missing or
        unparseable, so the caller falls back rather than recording a wrong number.
    """
    if not expiry:
        return None
    try:
        expiry_date = datetime.strptime(str(expiry).split(" ")[0], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (expiry_date - _today_ist()).days


async def is_expiry_day_from_api() -> bool:
    """
    Check if today is an expiry day by fetching the expiry list from Dhan API.
    
    Returns True if today's date appears in the expiry list.
    Falls back to the simple Tuesday check if the API call fails.
    """
    try:
        from config import settings
        from dhanhq import dhanhq

        try:
            from dhanhq import DhanContext
            context = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
            dhan = dhanhq(context)
        except ImportError:
            dhan = dhanhq(settings.dhan_client_id, settings.dhan_access_token)

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: dhan.expiry_list(
                under_security_id=int(settings.security_id),
                under_exchange_segment=settings.exchange_segment
            )
        )

        if response and response.get("status") == "success":
            expirations = response.get("data", {}).get("data", [])
            today_str = _today_ist().strftime("%Y-%m-%d")

            if today_str in expirations:
                return True

            # Also check if nearest expiry is today (handles format differences)
            if expirations:
                nearest = expirations[0]
                try:
                    nearest_date = datetime.strptime(nearest, "%Y-%m-%d").date()
                    if nearest_date == _today_ist():
                        return True
                except ValueError:
                    pass

            return False

        # API responded but not with success (e.g., auth failure) — fall back
        print(f"\033[93m[ARES] ⚠️ Expiry API returned non-success, falling back to Tuesday rule.\033[0m")
        return is_expiry_day_simple()

    except Exception as e:
        print(f"\033[93m[ARES] ⚠️ Expiry API check failed ({e}), falling back to Tuesday rule.\033[0m")
        return is_expiry_day_simple()
