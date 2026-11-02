"""
quote_service.py - Real-Time Market Quote Normalization and Persistence Service.

Provides a thread-safe in-memory cache and throttled Supabase persistence mechanism
for NIFTY underlying spot prices and the selected option contract's LTP, satisfying
the SpanQuant Ares frontend contract:
{
  "underlying": {
    "symbol": "NIFTY",
    "currentSpot": 23300.8,
    "updatedAt": "2026-11-02T04:52:00Z"
  },
  "selectedOption": {
    "expiry": "2026-11-03",
    "strike": 23300,
    "optionType": "CE",
    "securityId": "53940",
    "currentPremium": 125.5,
    "updatedAt": "2026-11-02T04:52:00Z"
  }
}

Security & Architecture:
- Avoids inserting a database row for every tick by performing throttled upserts on
  fixed primary keys ('UNDERLYING:NIFTY' and 'OPTION:SELECTED').
- Never exposes broker (Dhan) or database (Supabase service role) credentials to clients.
- Identifies option contracts strictly by expiry, strike, option type, and broker security ID.
  Never guesses or infers missing identifiers.
- Keeps live market quotes distinct from trade entry prices and detector signal spots.
"""

from copy import deepcopy
from datetime import datetime, timezone, timedelta
import logging
from math import isfinite
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ares.quotes")

IST = timezone(timedelta(hours=5, minutes=30))


def _to_utc_iso(ts: Any) -> Optional[str]:
    """Convert any datetime, epoch, or ISO timestamp into a normalized UTC ISO 8601 string.

    If the timestamp arrives as a naive datetime or string (e.g. from Dhan candle feed),
    it is explicitly treated as IST (UTC+5:30) per exchange convention, then converted to UTC.
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        if not isfinite(ts) or ts <= 0:
            return None
        dt = datetime.fromtimestamp(float(ts), timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(ts, datetime):
        dt = ts.replace(tzinfo=IST) if ts.tzinfo is None else ts
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(ts, str):
        text = ts.strip()
        if not text:
            return None
        try:
            # Handle trailing Z or z
            if text.endswith("Z") or text.endswith("z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            dt = dt.replace(tzinfo=IST) if dt.tzinfo is None else dt
            dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            return None
    return None


def _parse_utc_datetime(ts: Any) -> Optional[datetime]:
    """Parse a timestamp into a timezone-aware UTC datetime object."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts.replace(tzinfo=IST) if ts.tzinfo is None else ts
        return dt.astimezone(timezone.utc)
    iso_str = _to_utc_iso(ts)
    if iso_str:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return None


def validate_option_contract(contract: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate an option contract strictly across all 4 mandatory identifiers.

    All four fields are required:
      - expiry (or expiry_date): valid YYYY-MM-DD date string.
      - strike: finite positive float.
      - option_type (or optionType): 'CE' or 'PE'.
      - security_id (or securityId): non-empty string of digits.

    Returns a normalized dictionary if valid, or None if any identifier is missing or invalid.
    Never infers, guesses, or defaults missing fields.
    """
    if not isinstance(contract, dict):
        return None

    # Expiry
    expiry_val = contract.get("expiry") or contract.get("expiry_date")
    if not isinstance(expiry_val, str):
        return None
    expiry_str = expiry_val.strip()
    try:
        dt = datetime.strptime(expiry_str, "%Y-%m-%d")
        normalized_expiry = dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None

    # Strike
    strike_val = contract.get("strike")
    if strike_val is None or isinstance(strike_val, bool):
        return None
    try:
        strike_float = float(strike_val)
        if not isfinite(strike_float) or strike_float <= 0:
            return None
    except (ValueError, TypeError, OverflowError):
        return None

    # Option Type
    type_val = contract.get("option_type") or contract.get("optionType")
    if not isinstance(type_val, str):
        return None
    option_type = type_val.strip().upper()
    if option_type not in {"CE", "PE"}:
        return None

    # Security ID
    sec_id_val = contract.get("security_id") or contract.get("securityId")
    if sec_id_val is None or isinstance(sec_id_val, bool):
        return None
    security_id = str(sec_id_val).strip()
    if not security_id or not security_id.isdigit():
        return None

    return {
        "expiry": normalized_expiry,
        "strike": strike_float,
        "option_type": option_type,
        "security_id": security_id,
    }


def is_quote_stale(
    observed_at: Any,
    threshold_seconds: float = 10.0,
    reference_time: Optional[datetime] = None,
) -> bool:
    """Check whether a quote's observation timestamp exceeds the staleness threshold.

    @param observed_at: datetime, numeric epoch, or ISO timestamp.
    @param threshold_seconds: age threshold in seconds (default 10s).
    @param reference_time: current evaluation time (default UTC now).
    @return True if quote is older than threshold or invalid; False if fresh.
    """
    observed_dt = _parse_utc_datetime(observed_at)
    if observed_dt is None:
        return True
    now_dt = reference_time or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    else:
        now_dt = now_dt.astimezone(timezone.utc)

    age_seconds = (now_dt - observed_dt).total_seconds()
    return age_seconds > threshold_seconds


def classify_quote_freshness(
    observed_at: Any,
    fresh_threshold: float = 10.0,
    stale_threshold: float = 60.0,
    reference_time: Optional[datetime] = None,
) -> str:
    """Classify quote freshness into three UI tiers.

    - FRESH: <= 10.0s (active live market flow)
    - STALE: 10.0s < age <= 60.0s (lag or quiet market warning)
    - EXPIRED: > 60.0s or unparseable (feed disconnected or market closed)
    """
    observed_dt = _parse_utc_datetime(observed_at)
    if observed_dt is None:
        return "EXPIRED"
    now_dt = reference_time or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    else:
        now_dt = now_dt.astimezone(timezone.utc)

    age_seconds = (now_dt - observed_dt).total_seconds()
    if age_seconds <= fresh_threshold:
        return "FRESH"
    if age_seconds <= stale_threshold:
        return "STALE"
    return "EXPIRED"


def format_frontend_quote_contract(
    underlying: Optional[Dict[str, Any]],
    option: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Format market quotes into the stable SpanQuant Ares frontend contract.

    Guarantees strict JSON schema with typed fields and null handling.
    """
    underlying_obj = None
    if isinstance(underlying, dict) and underlying.get("price") is not None:
        price = float(underlying["price"])
        if isfinite(price) and price > 0:
            underlying_obj = {
                "symbol": str(underlying.get("symbol") or "NIFTY"),
                "currentSpot": price,
                "updatedAt": _to_utc_iso(underlying.get("observed_at")) or _to_utc_iso(datetime.now(timezone.utc)),
            }

    selected_option_obj = None
    if isinstance(option, dict) and option.get("price") is not None:
        validated = validate_option_contract(option)
        if validated is not None:
            price = float(option["price"])
            if isfinite(price) and price > 0:
                selected_option_obj = {
                    "expiry": validated["expiry"],
                    "strike": validated["strike"],
                    "optionType": validated["option_type"],
                    "securityId": validated["security_id"],
                    "currentPremium": price,
                    "updatedAt": _to_utc_iso(option.get("observed_at")) or _to_utc_iso(datetime.now(timezone.utc)),
                }

    return {
        "underlying": underlying_obj,
        "selectedOption": selected_option_obj,
    }


def match_option_quote_from_chain(
    contract: Dict[str, Any],
    full_chain: List[Dict[str, Any]],
    timestamp: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Match a pinned option contract against the live option chain to extract current LTP."""
    validated = validate_option_contract(contract)
    if validated is None or not full_chain:
        return None

    target_strike = validated["strike"]
    target_type = validated["option_type"]
    target_sec_id = validated["security_id"]

    for row in full_chain:
        strike_val = row.get("strike")
        if strike_val is None:
            continue
        try:
            if abs(float(strike_val) - target_strike) > 0.01:
                continue
        except (ValueError, TypeError):
            continue

        if target_type == "CE":
            sec_id = str(row.get("ce_security_id") or "")
            ltp = row.get("ce_ltp")
        else:
            sec_id = str(row.get("pe_security_id") or "")
            ltp = row.get("pe_ltp")

        if sec_id and sec_id == target_sec_id:
            try:
                price = float(ltp)
                if isfinite(price) and price > 0:
                    return {
                        **validated,
                        "price": price,
                        "observed_at": _to_utc_iso(timestamp) or _to_utc_iso(datetime.now(timezone.utc)),
                        "source": "dhan_option_chain",
                    }
            except (ValueError, TypeError):
                return None
    return None


class QuotePersistenceService:
    """Manages normalized market quotes and executes throttled Supabase upserts."""

    def __init__(self, supabase_client: Any = None, throttle_seconds: float = 1.0):
        self.supabase = supabase_client
        self.throttle_seconds = max(0.0, float(throttle_seconds))
        self._lock = threading.Lock()
        self._latest_underlying: Optional[Dict[str, Any]] = None
        self._latest_option: Optional[Dict[str, Any]] = None
        self._clear_option_pending: bool = False
        self._last_persist_time: float = 0.0
        self._dirty: bool = False

    def record_spot_quote(
        self,
        price: float,
        timestamp: Any,
        symbol: str = "NIFTY",
        source: str = "dhan_websocket_tick",
    ) -> bool:
        """Record latest underlying spot quote in memory."""
        if price is None or isinstance(price, bool):
            return False
        try:
            p = float(price)
            if not isfinite(p) or p <= 0:
                return False
        except (ValueError, TypeError, OverflowError):
            return False

        observed_iso = _to_utc_iso(timestamp) or _to_utc_iso(datetime.now(timezone.utc))
        with self._lock:
            self._latest_underlying = {
                "instrument_id": f"UNDERLYING:{symbol.upper()}",
                "instrument_type": "UNDERLYING",
                "symbol": symbol.upper(),
                "price": p,
                "observed_at": observed_iso,
                "source": str(source or "dhan_websocket_tick"),
            }
            self._dirty = True
        return True

    def record_option_quote(
        self,
        contract: Dict[str, Any],
        price: float,
        timestamp: Any,
        source: str = "dhan_quote_api",
    ) -> bool:
        """Record latest selected option quote in memory."""
        validated = validate_option_contract(contract)
        if validated is None:
            return False

        if price is None or isinstance(price, bool):
            return False
        try:
            p = float(price)
            if not isfinite(p) or p <= 0:
                return False
        except (ValueError, TypeError, OverflowError):
            return False

        observed_iso = _to_utc_iso(timestamp) or _to_utc_iso(datetime.now(timezone.utc))
        with self._lock:
            self._latest_option = {
                "instrument_id": "OPTION:SELECTED",
                "instrument_type": "OPTION",
                "symbol": "NIFTY",
                "price": p,
                "observed_at": observed_iso,
                "source": str(source or "dhan_quote_api"),
                "expiry": validated["expiry"],
                "strike": validated["strike"],
                "option_type": validated["option_type"],
                "security_id": validated["security_id"],
                "raw_metadata": deepcopy(contract),
            }
            self._clear_option_pending = False
            self._dirty = True
        return True

    def clear_selected_option(self) -> None:
        """Clear currently selected option quote when an active trade terminates."""
        with self._lock:
            if self._latest_option is not None:
                self._latest_option = None
                self._clear_option_pending = True
                self._dirty = True

    def get_latest_quotes_payload(self) -> Dict[str, Any]:
        """Return the stable frontend quote contract representing current in-memory quotes."""
        with self._lock:
            underlying = deepcopy(self._latest_underlying)
            option = deepcopy(self._latest_option)
        return format_frontend_quote_contract(underlying, option)

    async def flush(self, force: bool = False) -> bool:
        """Persist dirty quotes to Supabase table latest_quotes with throttling.

        Avoids per-tick database inserts by respecting the throttle duration.
        Returns True if a flush was performed, False otherwise.
        """
        if self.supabase is None:
            return False

        now = time.monotonic()
        with self._lock:
            if not self._dirty and not force:
                return False
            if not force and (now - self._last_persist_time) < self.throttle_seconds:
                return False

            rows_to_upsert = []
            if self._latest_underlying is not None:
                rows_to_upsert.append(deepcopy(self._latest_underlying))
            if self._latest_option is not None:
                rows_to_upsert.append(deepcopy(self._latest_option))

            should_delete_option = self._clear_option_pending
            self._dirty = False
            self._clear_option_pending = False
            self._last_persist_time = now

        try:
            if rows_to_upsert:
                # Perform atomic upsert on instrument_id primary key
                self.supabase.table("latest_quotes").upsert(rows_to_upsert).execute()
            if should_delete_option:
                self.supabase.table("latest_quotes").delete().eq("instrument_id", "OPTION:SELECTED").execute()
            return True
        except Exception as exc:
            logger.warning("Failed to persist latest quotes to Supabase: %s", exc)
            with self._lock:
                self._dirty = True  # Re-mark dirty on failure so next cycle retries
            return False


# Global default service instance
_global_quote_service: Optional[QuotePersistenceService] = None


def get_quote_service(supabase_client: Any = None, throttle_seconds: float = 1.0) -> QuotePersistenceService:
    """Retrieve or initialize the process-wide QuotePersistenceService singleton."""
    global _global_quote_service
    if _global_quote_service is None:
        if supabase_client is None:
            try:
                from storage import get_supabase_client
                supabase_client = get_supabase_client()
            except Exception:
                try:
                    from config import settings
                    from supabase import create_client
                    url = getattr(settings, "supabase_url", None)
                    key = (
                        getattr(settings, "supabase_service_role_key", None)
                        or getattr(settings, "supabase_key", None)
                    )
                    if url and key:
                        supabase_client = create_client(url, key)
                    else:
                        supabase_client = None
                except Exception:
                    supabase_client = None
        _global_quote_service = QuotePersistenceService(
            supabase_client=supabase_client,
            throttle_seconds=throttle_seconds,
        )
    elif supabase_client is not None and _global_quote_service.supabase is None:
        _global_quote_service.supabase = supabase_client
    return _global_quote_service
