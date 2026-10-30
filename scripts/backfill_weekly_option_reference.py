"""Dry-run-first recovery for the approved 26–30 Oct 2026 report cohort.

This one-time tool reads Supabase and Dhan history. It writes only when passed
``--apply`` and the installed, service-role-only guarded RPC accepts the exact
16-row cohort. It never sends a Discord message or changes capture settings.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from datetime import date, datetime, time as day_time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = str(Path(__file__).resolve().parents[1])
if REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, REPOSITORY_ROOT)

from config import settings
from execution.reference_prices import normalize_option_minutes
from oi_watch_context import IST, parse_candle_timestamp
from storage import AnalyticsLogger, load_dhan_credentials_from_supabase

START_IST = datetime(2026, 10, 26, tzinfo=IST)
END_IST = datetime(2026, 10, 31, tzinfo=IST)
START_UTC = START_IST.astimezone(timezone.utc).isoformat()
END_UTC = END_IST.astimezone(timezone.utc).isoformat()
EXPECTED_ROWS = 16
MARKET_OPEN = day_time(9, 15)
MARKET_CLOSE = day_time(15, 30)
REPORT_COLUMNS = (
    "id,setup_type,direction,pnl_points,score,result_state,market_context,"
    "entry_timestamp,exit_timestamp,time_metrics_excluded"
)


def _finite_decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{name} must be finite" + (" and positive" if positive else ""))
    return result


def _contract(row: dict) -> dict:
    sizing = (row.get("market_context") or {}).get("options_sizing")
    if not isinstance(sizing, dict):
        raise ValueError(f"{row.get('id')}: missing pinned options_sizing")
    option_type = sizing.get("option_type")
    if option_type not in {"CE", "PE"}:
        raise ValueError(f"{row.get('id')}: unsupported option type")
    expiry = date.fromisoformat(str(sizing.get("expiry_date")))
    security_id = str(sizing.get("security_id", ""))
    if not security_id.isdigit():
        raise ValueError(f"{row.get('id')}: invalid pinned security id")
    strike = _finite_decimal(sizing.get("strike"), "strike", positive=True)
    lot_size = _finite_decimal(sizing.get("lot_size"), "lot_size", positive=True)
    if lot_size != lot_size.to_integral_value():
        raise ValueError(f"{row.get('id')}: lot size must be an integer")
    return {
        "security_id": security_id,
        "exchange_segment": "NSE_FNO",
        "expiry_date": expiry.isoformat(),
        "strike": float(strike),
        "option_type": option_type,
        "lot_size": int(lot_size),
    }


def _event(row: dict, key: str) -> datetime:
    value = row.get(key)
    if not value:
        raise ValueError(f"{row.get('id')}: missing {key}")
    result = parse_candle_timestamp(value)
    if result.tzinfo is None:
        raise ValueError(f"{row.get('id')}: {key} has no timezone")
    local = result.astimezone(IST)
    if not START_IST <= local < END_IST:
        raise ValueError(f"{row.get('id')}: {key} is outside the approved week")
    if not MARKET_OPEN <= local.time().replace(tzinfo=None) < MARKET_CLOSE:
        raise ValueError(f"{row.get('id')}: {key} is outside market hours")
    return result


def _report_eligible(row: dict) -> bool:
    if row.get("time_metrics_excluded") is True or row.get("pnl_points") is None:
        return False
    entry, exit_at = row.get("entry_timestamp"), row.get("exit_timestamp")
    if entry and exit_at and parse_candle_timestamp(exit_at) < parse_candle_timestamp(entry):
        return False
    return True


def _load_cohort(supabase) -> list[dict]:
    result: list[dict] = []
    offset = 0
    while True:
        response = (
            supabase.table("trade_analytics")
            .select(REPORT_COLUMNS)
            .gte("exit_timestamp", START_UTC)
            .lt("exit_timestamp", END_UTC)
            .eq("time_metrics_excluded", False)
            .order("exit_timestamp")
            .order("id")
            .range(offset, offset + 999)
            .execute()
        )
        page = getattr(response, "data", None) or []
        result.extend(row for row in page if _report_eligible(row))
        if len(page) < 1000:
            break
        offset += 1000
    if len(result) != EXPECTED_ROWS:
        raise RuntimeError(f"Expected exactly 16 report-eligible trades for 26–30 Oct; found {len(result)}")
    return result


def _expected_bar(rows: dict[datetime, dict], event_at: datetime, contract: dict) -> dict:
    local = event_at.astimezone(IST)
    minute = local.replace(second=0, microsecond=0)
    bar = rows.get(minute)
    if not bar or not bar.get("complete"):
        raise ValueError(f"No complete Dhan 1-minute close at {minute.isoformat()}")
    if parse_candle_timestamp(bar["sampled_at"]) < minute + timedelta(minutes=1):
        raise ValueError("Dhan minute was not sampled after its close")
    if "strike" in bar and Decimal(str(bar["strike"])) != Decimal(str(contract["strike"])):
        raise ValueError(
            f"Dhan rolling series returned strike {bar['strike']} instead of pinned {contract['strike']}"
        )
    return bar


class DhanHistory:
    """Fetch exact-security option bars; fail closed when only rolling expiries remain."""

    def __init__(self, client, *, now: datetime | None = None):
        self.client = client
        self.now = now or datetime.now(timezone.utc)
        self._last_request = 0.0
        self._cache: dict[tuple, dict[datetime, dict]] = {}

    def _throttle(self):
        remaining = 1.05 - (time.monotonic() - self._last_request)
        if self._last_request and remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()

    def _active_rows(self, contract: dict, day: date) -> dict[datetime, dict]:
        key = ("active", contract["security_id"], day.isoformat())
        if key not in self._cache:
            self._throttle()
            requested_at = datetime.now(timezone.utc)
            response = self.client.intraday_minute_data(
                int(contract["security_id"]), "NSE_FNO", "OPTIDX",
                day.isoformat(), day.isoformat(), interval=1,
            )
            if not isinstance(response, dict) or response.get("status") != "success":
                raise RuntimeError("Dhan exact-security minute-history request failed")
            rows = normalize_option_minutes(response.get("data"), day, requested_at)
            self._cache[key] = {
                parse_candle_timestamp(item["timestamp"]).astimezone(IST): {
                    "bar_start": item["timestamp"], "sampled_at": item["sampled_at"],
                    "complete": item["complete"], "open": item["open"], "high": item["high"],
                    "low": item["low"], "close": item["close"], "volume": item["volume"],
                }
                for item in rows
            }
        return self._cache[key]

    def mark(self, contract: dict, event_at: datetime) -> dict:
        local = event_at.astimezone(IST)
        expiry = date.fromisoformat(contract["expiry_date"])
        if expiry >= self.now.astimezone(IST).date():
            rows = self._active_rows(contract, local.date())
            bar = _expected_bar(rows, event_at, contract)
            provider = "dhan_intraday_minute"
            provenance = {
                "provider": provider,
                "requested_security_id": contract["security_id"],
                **_requested_pin(contract), **_bar_provenance(bar),
            }
        else:
            raise ExpiredContractPinUnavailable(
                f"Dhan history cannot prove the pinned {expiry.isoformat()} expiry after it expires; "
                "expiry_code=0 for current/near expiry is rejected, while expiry_code=1 is next expiry"
            )
        observed_at = (local.replace(second=0, microsecond=0) + timedelta(minutes=1)).isoformat()
        return {
            "price": bar["close"], "observed_at": observed_at,
            "source": "completed_1m_close", "approximate": True,
            "provenance": provenance,
        }


class ExpiredContractPinUnavailable(ValueError):
    """Dhan history cannot prove that a rolling bar belongs to the pinned expiry."""


def _requested_pin(contract: dict) -> dict:
    return {
        "requested_exchange_segment": "NSE_FNO",
        "requested_expiry_date": contract["expiry_date"],
        "requested_strike": contract["strike"],
        "requested_option_type": contract["option_type"],
        "requested_lot_size": contract["lot_size"],
    }


def _bar_provenance(bar: dict) -> dict:
    return {
        "bar_start": bar["bar_start"], "sampled_at": bar["sampled_at"],
        "complete": bar["complete"], "open": bar["open"], "high": bar["high"],
        "low": bar["low"], "close": bar["close"], "volume": bar["volume"],
    }


def _build_candidates(rows: list[dict], history: DhanHistory) -> list[dict]:
    payload = []
    for row in rows:
        contract = _contract(row)
        entry_at = _event(row, "entry_timestamp")
        exit_at = _event(row, "exit_timestamp")
        result_state = row.get("result_state")
        if not result_state or result_state == "OPEN" or exit_at < entry_at:
            raise ValueError(f"{row['id']}: report row is not a valid closed trade")
        try:
            entry_mark = history.mark(contract, entry_at)
            exit_mark = history.mark(contract, exit_at)
            missing_reason = None
        except ExpiredContractPinUnavailable as exc:
            entry_mark = exit_mark = None
            missing_reason = str(exc)
        payload.append({
            "id": row["id"], "contract": contract,
            "entry_event_at": entry_at.isoformat(), "exit_event_at": exit_at.isoformat(),
            "exit_reason": result_state,
            "entry": entry_mark, "exit": exit_mark, "missing_reason": missing_reason,
        })
    return payload


def _preview(rows: list[dict], payload: list[dict]) -> dict:
    trades = {str(row["id"]): row for row in rows}
    points = Decimal("0")
    rupees = Decimal("0")
    priced = 0
    for item in payload:
        if not isinstance(item.get("entry"), dict) or not isinstance(item.get("exit"), dict):
            continue
        delta = Decimal(str(item["exit"]["price"])) - Decimal(str(item["entry"]["price"]))
        lot_size = Decimal(str(item["contract"]["lot_size"]))
        points += delta
        rupees += delta * lot_size
        priced += 1
    complete = len(trades) == EXPECTED_ROWS and priced == EXPECTED_ROWS
    return {
        "trades": len(trades), "priced": priced, "approximate": priced,
        "net_option_premium_points": float(points) if complete else None,
        "one_lot_option_pnl_rupees": float(rupees) if complete else None,
        "unpriced_trade_ids": [item["id"] for item in payload
                               if not isinstance(item.get("entry"), dict)
                               or not isinstance(item.get("exit"), dict)],
        "unpriced_reasons": {item["id"]: item["missing_reason"] for item in payload
                             if item.get("missing_reason")},
    }


def _report_preview(rows: list[dict], payload: list[dict]) -> dict:
    """Render the exact report formatter against in-memory candidate evidence."""
    from reports import build_report_embed, compute_metrics

    by_id = {str(item["id"]): item for item in payload}
    preview_rows = copy.deepcopy(rows)
    for row in preview_rows:
        item = by_id[str(row["id"])]
        if not isinstance(item.get("entry"), dict) or not isinstance(item.get("exit"), dict):
            continue
        premium_points = item["exit"]["price"] - item["entry"]["price"]
        row.setdefault("market_context", {})["option_reference"] = {
            "formula_version": "one_lot_hold_to_exit_v1", "revision": 4,
            "status": "CLOSED", "reference_lots": 1,
            "contract": item["contract"],
            "entry_event_at": item["entry_event_at"], "entry": item["entry"],
            "exit_event_at": item["exit_event_at"], "exit_reason": item["exit_reason"],
            "exit": item["exit"], "premium_points": premium_points, "milestones": {},
        }
    return build_report_embed(
        "WEEKLY", "26 Oct – 30 Oct 2026", compute_metrics(preview_rows),
    )


def _verify_persisted(supabase) -> dict:
    rows = _load_cohort(supabase)
    references = []
    for row in rows:
        context = row.get("market_context") or {}
        reference = context.get("option_reference") or {}
        if (reference.get("formula_version") != "one_lot_hold_to_exit_v1"
                or reference.get("status") != "CLOSED"
                or not isinstance(reference.get("entry"), dict)
                or not isinstance(reference.get("exit"), dict)
                or reference["entry"].get("approximate") is not True
                or reference["exit"].get("approximate") is not True):
            raise RuntimeError(f"{row['id']}: persisted weekly reference is incomplete or not approximate")
        references.append(reference)
    points = sum(Decimal(str(ref["exit"]["price"])) - Decimal(str(ref["entry"]["price"]))
                 for ref in references)
    rupees = sum((Decimal(str(ref["exit"]["price"])) - Decimal(str(ref["entry"]["price"])))
                 * Decimal(str(ref["contract"]["lot_size"])) for ref in references)
    return {"trades": len(rows), "priced": len(references), "approximate": len(references),
            "net_option_premium_points": float(points), "one_lot_option_pnl_rupees": float(rupees)}


def run(*, apply: bool = False) -> dict:
    if not settings.supabase_url or not settings.supabase_key:
        raise RuntimeError("Supabase URL and key are required")
    analytics = AnalyticsLogger()
    cohort = _load_cohort(analytics.supabase)
    if apply:
        if not getattr(analytics, "_option_backend_configured", False):
            raise RuntimeError("--apply requires SUPABASE_SERVICE_ROLE_KEY")
        if not analytics.option_reference_capture_ready():
            raise RuntimeError("--apply requires the merged and installed option-reference migration")

    load_dhan_credentials_from_supabase()
    if not settings.dhan_client_id or not settings.dhan_access_token:
        raise RuntimeError("Dhan credentials were not available from Supabase")
    from fetchers.price_fetcher import PriceFetcher

    history = DhanHistory(PriceFetcher().dhan)
    payload = _build_candidates(cohort, history)
    result = _preview(cohort, payload)
    result["report_preview"] = _report_preview(cohort, payload)
    result["mode"] = "apply" if apply else "dry-run"
    if apply:
        if result["priced"] != EXPECTED_ROWS:
            raise RuntimeError(
                f"Refusing partial backfill: only {result['priced']}/16 trades have verified matching-contract marks"
            )
        response = analytics.apply_option_reference_weekly_backfill(payload)
        if response.get("backfilled") != EXPECTED_ROWS or response.get("success") is not True:
            raise RuntimeError("Supabase did not confirm all 16 weekly backfill rows")
        result["rpc_idempotent"] = response.get("idempotent") is True
        result["verified"] = _verify_persisted(analytics.supabase)
        if result["verified"]["priced"] != EXPECTED_ROWS:
            raise RuntimeError("Post-write verification found incomplete option-price coverage")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write only through the guarded service-role RPC")
    args = parser.parse_args()
    try:
        print(json.dumps(run(apply=args.apply), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        message = str(exc)
        for secret in (getattr(settings, "dhan_client_id", ""),
                       getattr(settings, "dhan_access_token", ""),
                       getattr(settings, "supabase_key", ""),
                       getattr(settings, "supabase_service_role_key", "")):
            if secret:
                message = message.replace(secret, "[redacted]")
        print(f"Weekly option-reference backfill failed: {type(exc).__name__}: {message}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
