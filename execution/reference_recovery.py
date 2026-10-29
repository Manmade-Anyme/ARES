"""Recover missing reference-accounting prices outside the protection loop."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging

from fetchers.price_fetcher import ReferencePriceDeferred
from oi_watch_context import parse_candle_timestamp

log = logging.getLogger(__name__)


async def _retry_deferred(request):
    """Retry in the background after protection's provider cooldown."""
    for attempt in range(3):
        try:
            return await request()
        except ReferencePriceDeferred:
            if attempt == 2:
                raise
            await asyncio.sleep(1.1)


def _stamp(value):
    try:
        return parse_candle_timestamp(value)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _option(trade):
    if not isinstance(trade, dict):
        return None
    context = trade.get("market_context") or {}
    option = context.get("option_trailing") or {}
    if (option.get("policy_version") != "option_atr_v1"
            or option.get("accounting_formula_version") != "one_lot_reference_v1"):
        return None
    return option


def _closed_trade(row):
    if not isinstance(row, dict) or not isinstance(row.get("active_trade"), dict):
        return None
    trade = deepcopy(row["active_trade"])
    opt = deepcopy(row.get("option_trailing"))
    if not isinstance(opt, dict):
        return None
    trade["market_context"] = {"option_trailing": opt}
    if (_option(trade) is None or opt.get("phase") != "CLOSED"
            or trade.get("state") not in {"CLOSED", "STOPPED_OUT"}
            or row.get("result_state") in {None, "OPEN"}
            or not trade.get("id") or trade.get("direction") not in {"BULLISH", "BEARISH"}
            or type(opt.get("revision")) is not int or opt["revision"] < 1):
        return None
    return trade


def _new_evidence(before, after, row):
    old = (before or {}).get("legs", {})
    new = (after or {}).get("legs", {})
    if any(leg.get(market + "_price") is not None
           and (old.get(name) or {}).get(market + "_price") is None
           for name, leg in new.items() for market in ("spot", "option")):
        return True
    for market, column in (("spot", "pnl_points"), ("option", "option_pnl_points")):
        key = market + "_total_points"
        if after.get(key) is not None and ((before or {}).get(key) is None or row.get(column) is None):
            return True
        booked = market + "_booked_points"
        if after.get(booked) is not None and (before or {}).get(booked) is None:
            return True
    return False


def _merge_minutes(old, new, *, preserve=()):
    rows = {}
    for row in list(old or []) + list(new or []):
        if not isinstance(row, dict):
            continue
        at, sampled = _stamp(row.get("timestamp")), _stamp(row.get("sampled_at"))
        if at is None or sampled is None:
            continue
        previous = rows.get(at)
        if previous is None or sampled >= _stamp(previous.get("sampled_at")):
            rows[at] = row
    retained = set(sorted(rows)[-2000:])
    retained.update(at for value in preserve if (at := _stamp(value)) in rows)
    return [rows[at] for at in sorted(retained)]


def _complete_after_correction(row, option, accounting):
    """Mirror the incomplete-scan predicates to adjust for removed rows."""
    if option.get("original_option_entry") is None:
        return False
    for market, column in (("spot", "pnl_points"), ("option", "option_pnl_points")):
        if row.get(column) is None and accounting.get(market + "_total_points") is None:
            return False
    legs = accounting.get("legs") or {}
    highest = option.get("highest_milestone") or 0
    required = ["EXIT"] + (["T1"] if highest >= 1 else []) + (["T2"] if highest >= 2 else [])
    return all((legs.get(name) or {}).get(market + "_price") is not None
               for name in required for market in ("spot", "option"))


async def run_reference_recovery(manager, fetcher, analytics, *, limit=100, offset=None, max_pages=3):
    """One bounded pass; schedule as a single non-overlapping background task.

    Network, database reads and correction writes are awaited only within this
    task. The trading monitor must not wait for this function. A bounded set of
    pages is read before corrections, then the cursor advances on the manager.
    The cursor wraps, so concurrent offset shifts cannot permanently starve rows.
    """
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if type(max_pages) is not int or max_pages < 1:
        raise ValueError("max_pages must be positive")
    if offset is not None and (type(offset) is not int or offset < 0):
        raise ValueError("offset must be a non-negative integer")
    start = offset if offset is not None else getattr(manager, "_reference_recovery_offset", 0)
    rows = []
    scan_ok = False
    reached_end = False
    try:
        page_offset = start
        for _ in range(max_pages):
            page = await asyncio.to_thread(analytics.list_incomplete_option_accounting,
                                           limit=limit, offset=page_offset)
            if not isinstance(page, list):
                raise ValueError("Invalid reference recovery page")
            rows.extend(page)
            if len(page) < limit:
                reached_end = True
                break
            page_offset += len(page)
        scan_ok = True
    except Exception:
        log.warning("Reference accounting recovery query failed", exc_info=True)
        rows = []
    closed = [(row, trade) for row in (rows or []) if (trade := _closed_trade(row)) is not None]
    active = [deepcopy(trade) for trade in list(manager.active_trades) if _option(trade) is not None]
    option_requests, spot_requests, quote_requests = {}, {}, {}
    required_option_minutes, required_spot_minutes = {}, set()
    now = datetime.now(timezone.utc)
    for trade in active + [trade for _, trade in closed]:
        opt = _option(trade)
        contract = opt.get("contract") or {}
        security = str(contract.get("security_id") or "")
        legs = (opt.get("accounting") or {}).get("legs") or {}
        for name, leg in legs.items():
            if not isinstance(leg, dict):
                continue
            event = _stamp(leg.get("source_minute") or leg.get("event_at"))
            if event is None:
                continue
            if leg.get("option_price") is None and security:
                # Response contains the whole session; fetch once per pinned contract/date.
                key = (security, str(contract.get("exchange_segment") or contract.get("exchange")),
                       str(contract.get("expiry_date") or contract.get("expiry")),
                       str(contract.get("strike")), str(contract.get("option_type")), event.date())
                option_requests[key] = (contract, event)
                required_option_minutes.setdefault(security, set()).add(
                    event.replace(second=0, microsecond=0))
                exact = _stamp(leg.get("event_at")) if not leg.get("source_minute") else None
                if exact is not None and 0 <= (now - exact).total_seconds() <= 5:
                    quote_requests[key[:-1]] = contract
            if name == "EXIT" and leg.get("spot_price") is None and leg.get("option_source") == "completed_5m_close":
                spot_requests[event.date()] = event
                required_spot_minutes.add((event - timedelta(minutes=1)).replace(second=0, microsecond=0))
    option_minutes, option_quotes, spot_minutes = {}, {}, []
    for key, contract in quote_requests.items():
        try:
            quote = await _retry_deferred(lambda: fetcher.fetch_option_observation(contract))
            if quote:
                option_quotes.setdefault(key[0], []).append(quote)
        except ReferencePriceDeferred:
            pass
        except Exception:
            log.warning("Option reference quote unavailable", exc_info=True)
    # A pass may receive only one request slot. Rotate pending histories so a
    # persistently unavailable option bar cannot starve the spot exit price.
    requests = [("option", key, value) for key, value in option_requests.items()]
    requests += [("spot", date, event) for date, event in spot_requests.items()]
    if requests:
        first = getattr(manager, "_reference_fetch_cursor", 0) % len(requests)
        manager._reference_fetch_cursor = (first + 1) % len(requests)
        requests = requests[first:] + requests[:first]
    for kind, key, value in requests:
        try:
            if kind == "option":
                contract, event = value
                bars = await _retry_deferred(lambda: fetcher.fetch_option_minutes(contract, event))
                option_minutes.setdefault(key[0], []).extend(bars or [])
            else:
                spot_minutes.extend(await _retry_deferred(lambda: fetcher.fetch_spot_minutes(value)) or [])
        except ReferencePriceDeferred:
            pass
        except Exception:
            log.warning("%s reference history unavailable", kind.capitalize(), exc_info=True)
    # A concurrent market observer may already hold a useful quote. Preserve
    # that evidence rather than clearing it merely because a fetch failed.
    existing = getattr(manager, "_reference_price_evidence", {})
    for key, value in (existing.get("option_minutes") or {}).items():
        option_minutes[key] = _merge_minutes(value, option_minutes.get(key),
                                              preserve=required_option_minutes.get(key, ()))
    for key, value in list(option_minutes.items()):
        if key not in (existing.get("option_minutes") or {}):
            option_minutes[key] = _merge_minutes((), value,
                                                  preserve=required_option_minutes.get(key, ()))
    for key, value in (existing.get("option_quotes") or {}).items():
        option_quotes[key] = (list(value) + option_quotes.get(key, []))[-100:]
    spot_minutes = _merge_minutes(existing.get("spot_minutes"), spot_minutes,
                                  preserve=required_spot_minutes)
    manager.set_reference_price_evidence(option_minutes=option_minutes, option_quotes=option_quotes,
                                         spot_minutes=spot_minutes)
    try:
        await manager.enrich_reference_accounting()
    except Exception:
        log.warning("Open reference accounting recovery failed", exc_info=True)
    corrected = 0
    removed = 0
    for row, trade in closed:
        opt = _option(trade)
        before = deepcopy(opt.get("accounting"))
        accounting = before
        correction_succeeded = False
        try:
            accounting = manager._enrich_reference_trade(trade)
            if _new_evidence(before, accounting, row):
                revision = opt["revision"]
                payload = json.dumps(accounting, sort_keys=True, separators=(",", ":"), allow_nan=False)
                identity = hashlib.sha256(f'{trade["id"]}:{revision}:{payload}'.encode()).hexdigest()
                await asyncio.to_thread(analytics.apply_option_accounting_correction,
                                        trade["id"], revision, "reference-recovery-" + identity,
                                        accounting, accounting.get("spot_total_points"),
                                        accounting.get("option_total_points"))
                corrected += 1
                correction_succeeded = True
        except Exception:
            log.warning("Closed reference accounting correction deferred", exc_info=True)
        # Label sync is independent: a missed snapshot is retried even when no
        # price evidence changed or a correction raced with another writer.
        try:
            label = await asyncio.to_thread(analytics.sync_option_reference_ml_label, trade["id"])
            synced = isinstance(label, dict) and label.get("success") is True and label.get("matched", 0) > 0
            stable_accounting = accounting if correction_succeeded else before
            totals_persisted = correction_succeeded or (row.get("pnl_points") is not None
                                                        and row.get("option_pnl_points") is not None)
            if synced and totals_persisted and _complete_after_correction(row, opt, stable_accounting or {}):
                removed += 1
        except Exception:
            log.warning("Closed reference ML label sync deferred", exc_info=True)
    if scan_ok:
        manager._reference_recovery_offset = 0 if reached_end else max(0, start + len(rows) - removed)
    return {"rows_seen": len(rows or []), "corrected": corrected}
