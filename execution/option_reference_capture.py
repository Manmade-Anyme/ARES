"""Independent observed-premium capture for weekly report reference P&L.

This worker has no option-stop or trade-lifecycle responsibilities. It only
captures exact-contract reference marks and spot milestone context through the
dedicated versioned RPCs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from config import settings
from execution.reference_prices import option_target_evidence
from oi_watch_context import parse_candle_timestamp

log = logging.getLogger(__name__)

FORMULA_VERSION = "one_lot_hold_to_exit_v1"
DEFAULT_SCAN_LIMIT = 100
MAX_ROWS_PER_PASS = 8


def _iso(value):
    try:
        return parse_candle_timestamp(value).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _reference(trade):
    context = trade.get("market_context") or {}
    value = context.get("option_reference") if isinstance(context, dict) else None
    return value if isinstance(value, dict) and value.get("formula_version") == FORMULA_VERSION else None


def _contract_key(contract):
    return tuple(str(contract.get(key) or "") for key in (
        "security_id", "exchange_segment", "expiry_date", "strike", "option_type",
    ))


def _event_id(event):
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"option-reference-{digest}"


def _has_leg(reference, name):
    return isinstance(reference.get(name), dict)


def _has_pending_exit(reference):
    return bool(reference.get("exit_event_at") and reference.get("exit_reason"))


def _evidence(event_at, *, quote, bars, now):
    return option_target_evidence(
        event_at,
        observations=[quote] if isinstance(quote, dict) else (),
        option_minutes=bars or (),
        observed_at=now,
    )


async def _quote(fetcher, contract, key, cache):
    if key not in cache:
        try:
            cache[key] = await fetcher.fetch_option_observation(contract)
        except Exception as exc:
            # Reference observations yield to live market protection; a later
            # pass can use the completed minute close instead.
            cache[key] = None
            log.debug("Pinned option quote deferred for %s: %s", key[0], exc)
    return cache[key]


async def _minutes(fetcher, contract, event_at, cache):
    event = parse_candle_timestamp(event_at)
    key = (*_contract_key(contract), event.date().isoformat())
    if key not in cache:
        try:
            cache[key] = await fetcher.fetch_option_minutes(contract, event)
        except Exception as exc:
            cache[key] = []
            log.debug("Pinned option minute history deferred for %s: %s", key[0], exc)
    return cache[key]


async def _price_evidence(fetcher, contract, event_at, now, quote_cache, minute_cache):
    event = parse_candle_timestamp(event_at)
    quote = None
    if abs((now - event).total_seconds()) <= 5:
        quote = await _quote(fetcher, contract, _contract_key(contract), quote_cache)
        selected = _evidence(event, quote=quote, bars=(), now=now)
        if selected is not None:
            return selected
    if event.replace(second=0, microsecond=0) + timedelta(minutes=1) > now:
        return None
    bars = await _minutes(fetcher, contract, event, minute_cache)
    validation_now = datetime.now(timezone.utc)
    return _evidence(event, quote=quote, bars=bars, now=validation_now)


def _apply_sync(manager, analytics, trade_id, reference, event):
    """Use the same per-trade lock as generic market_context writers."""
    locks = getattr(manager, "_context_locks", None)
    if locks is None:
        locks = {}
        manager._context_locks = locks
    with locks.setdefault(str(trade_id), threading.Lock()):
        expected = reference.get("revision")
        if type(expected) is not int or expected < 1:
            raise ValueError("Option reference state has an invalid revision")
        return analytics.apply_option_reference_capture(
            str(trade_id), expected, _event_id(event), event,
        )


async def _persist_event(manager, analytics, trade, reference, event):
    result = await asyncio.to_thread(
        _apply_sync, manager, analytics, trade["id"], reference, event,
    )
    updated = result.get("option_reference")
    if isinstance(updated, dict):
        reference.clear()
        reference.update(deepcopy(updated))
        context = trade.setdefault("market_context", {})
        context["option_reference"] = deepcopy(updated)
    return result


def _row_from_active(trade):
    reference = _reference(trade)
    if reference is None:
        return None
    return {
        "id": trade.get("id"),
        "entry_timestamp": trade.get("entry_timestamp") or reference.get("entry_event_at"),
        "exit_timestamp": trade.get("exit_timestamp"),
        "result_state": trade.get("result_state"),
        "market_context": deepcopy(trade.get("market_context") or {}),
    }


def _pending_for(manager, trade_id):
    pending = getattr(manager, "_pending_option_reference_events", {})
    events = pending.get(str(trade_id), [])
    return list(events) if isinstance(events, list) else []


def _remove_pending(manager, trade_id, event):
    pending = getattr(manager, "_pending_option_reference_events", {})
    key = str(trade_id)
    events = pending.get(key, [])
    identity = _event_id(event)
    remaining = [item for item in events if _event_id(item) != identity]
    if remaining:
        pending[key] = remaining
    else:
        pending.pop(key, None)


def _durable_milestone_events(context, reference):
    """Rebuild capture events from the separately persisted spot milestone mirror."""
    captured = reference.get("milestones") or {}
    stored = context.get("option_reference_milestones") or {}
    events = []
    for name in ("T1", "T2"):
        marker = stored.get(name) if isinstance(stored, dict) else None
        if name in captured or not isinstance(marker, dict):
            continue
        if not all(marker.get(key) is not None for key in (
            "event_at", "spot_target", "source", "time_basis",
        )):
            continue
        events.append({"kind": "MILESTONE", "name": name, "milestone": {
            key: marker.get(key) for key in (
                "event_at", "spot_target", "spot_price", "source", "time_basis",
            )
        }})
    return events


async def _process_row(manager, fetcher, analytics, row, quote_cache, minute_cache, now):
    trade_id = str(row.get("id") or "")
    context = row.get("market_context") or {}
    reference = context.get("option_reference") if isinstance(context, dict) else None
    if not trade_id or not isinstance(reference, dict) or reference.get("formula_version") != FORMULA_VERSION:
        return 0
    contract = reference.get("contract")
    if not isinstance(contract, dict):
        return 0
    if not reference.get("entry_event_at"):
        return 0
    # Keep a process-local trade snapshot in sync after every committed RPC.
    trade = next((item for item in getattr(manager, "active_trades", [])
                  if str(item.get("id")) == trade_id), None)
    if trade is None:
        trade = {"id": trade_id, "market_context": deepcopy(context)}
    else:
        trade.setdefault("market_context", {})["option_reference"] = deepcopy(reference)
    reference = trade["market_context"]["option_reference"]
    completed = 0

    milestone_events = _pending_for(manager, trade_id)
    milestone_events.extend(_durable_milestone_events(context, reference))
    seen_milestones = set()
    for event in milestone_events:
        if event.get("kind") != "MILESTONE":
            continue
        identity = _event_id(event)
        if identity in seen_milestones:
            continue
        seen_milestones.add(identity)
        existing = (reference.get("milestones") or {}).get(event.get("name"))
        if isinstance(existing, dict):
            _remove_pending(manager, trade_id, event)
            continue
        try:
            await _persist_event(manager, analytics, trade, reference, event)
            _remove_pending(manager, trade_id, event)
            completed += 1
        except Exception:
            log.warning("Option spot milestone capture deferred for trade %s", trade_id, exc_info=True)
            break

    entry_at = row.get("entry_timestamp") or reference.get("entry_event_at")
    if not _has_leg(reference, "entry"):
        try:
            evidence = await _price_evidence(fetcher, contract, entry_at, now, quote_cache, minute_cache)
            if evidence:
                event = {"kind": "ENTRY_CAPTURE", "event_at": _iso(entry_at), "evidence": evidence}
                await _persist_event(manager, analytics, trade, reference, event)
                completed += 1
        except Exception:
            log.warning("Option entry premium capture deferred for trade %s", trade_id, exc_info=True)

    exit_at = row.get("exit_timestamp")
    exit_reason = row.get("result_state")
    if exit_at and exit_reason and exit_reason != "OPEN":
        marker = {"kind": "EXIT_MARKER", "event_at": _iso(exit_at), "exit_reason": str(exit_reason)}
        try:
            if not _has_pending_exit(reference):
                await _persist_event(manager, analytics, trade, reference, marker)
                completed += 1
            if not _has_leg(reference, "exit"):
                evidence = await _price_evidence(fetcher, contract, exit_at, now, quote_cache, minute_cache)
                if evidence:
                    capture = {
                        "kind": "EXIT_CAPTURE", "event_at": _iso(exit_at),
                        "exit_reason": str(exit_reason), "evidence": evidence,
                    }
                    await _persist_event(manager, analytics, trade, reference, capture)
                    completed += 1
        except Exception:
            log.warning("Option final-exit premium capture deferred for trade %s", trade_id, exc_info=True)
    return completed


async def run_option_reference_capture(manager, fetcher, analytics, *, limit=DEFAULT_SCAN_LIMIT,
                                       max_rows=MAX_ROWS_PER_PASS):
    """Persist queued spot milestones and capture missing option entry/exit marks."""
    if (not getattr(settings, "option_reference_accounting_enabled", False)
            or fetcher is None or getattr(analytics, "_option_backend_configured", False) is not True):
        return {"rows_seen": 0, "events_applied": 0}
    try:
        ready = await asyncio.to_thread(analytics.option_reference_capture_ready)
    except Exception:
        log.warning("Option reference capture capability check failed", exc_info=True)
        return {"rows_seen": 0, "events_applied": 0}
    if ready is not True:
        return {"rows_seen": 0, "events_applied": 0}

    offset = getattr(manager, "_option_reference_recovery_offset", 0)
    try:
        rows = await asyncio.to_thread(analytics.list_incomplete_option_reference, limit, offset)
    except Exception:
        log.warning("Option reference recovery scan failed", exc_info=True)
        rows = []
    rows_by_id = {str(row.get("id")): row for row in rows if row.get("id") is not None}
    for trade in getattr(manager, "active_trades", []):
        local = _row_from_active(trade)
        if local is None:
            continue
        key = str(local.get("id") or "")
        if key in getattr(manager, "_pending_option_reference_events", {}):
            rows_by_id.setdefault(key, local)
    selected = list(rows_by_id.values())[:max_rows]
    if rows:
        next_offset = offset + min(len(rows), max_rows)
        manager._option_reference_recovery_offset = (
            next_offset if len(rows) == limit or next_offset < offset + len(rows) else 0
        )
    else:
        manager._option_reference_recovery_offset = 0
    now = datetime.now(timezone.utc)
    quote_cache, minute_cache = {}, {}
    applied = 0
    for row in selected:
        try:
            applied += await _process_row(manager, fetcher, analytics, row,
                                          quote_cache, minute_cache, now)
        except Exception:
            log.warning("Option reference capture pass deferred for trade %s", row.get("id"), exc_info=True)
    return {"rows_seen": len(selected), "events_applied": applied}
