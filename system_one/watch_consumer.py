"""Independent ready-watch inference and single-message Discord delivery.

Each loop owns its database client. PostgreSQL controls deadlines and claims;
an inference invocation or ambiguous webhook is never replayed after a crash.
No trading modules, signal questions or confirmed-trade delivery are imported.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
import math
import os
from threading import Event, Thread
import time
from typing import Any, Callable
import uuid

import httpx
from config_profiles import OI_WATCH_JEV_CONFIG

from .watch_prediction import FIELD_NAME, format_watch_prediction, invoke_watch_jev

logger = logging.getLogger("system_one.watch_consumer")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))


def _rows(client: Any, name: str, params: dict | None = None) -> list[dict]:
    data = client.rpc(name, params or {}).execute().data
    return [data] if isinstance(data, dict) else (data or [])


def _budget(row: dict, elapsed: float) -> float:
    try:
        seconds = float(row["remaining_seconds"]) - elapsed
        return seconds if math.isfinite(seconds) and seconds > 0 else 0.0
    except (KeyError, TypeError, ValueError):
        return 0.0


def compose_watch_payload(row: dict) -> dict:
    """Copy the frozen original embed and append exactly one prediction field.

    Cancellation embeds keep their legacy format. Only the explicit restart
    time marker is filled; real cancellation timestamps remain untouched.
    """
    if row.get("is_cancellation"):
        payload = deepcopy(row["cancellation_payload"])
        stamp = datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S IST")
        for embed in payload.get("embeds", []):
            for field in embed.get("fields", []):
                if field.get("value") == "__WORKER_TIME__":
                    field["value"] = stamp
        return payload
    payload = deepcopy(row["base_payload"])
    prediction = row.get("prediction") if (row.get("prediction_status") == "COMPLETED"
        and row.get("prediction_available_within_deadline") is True) else None
    payload["embeds"][0]["fields"].append({
        "name": FIELD_NAME,
        "value": format_watch_prediction(row["input_state"], prediction),
        "inline": False,
    })
    return payload


def _complete_inference(client: Any, event_id: str, token: str, prediction: dict | None, error: str | None) -> bool:
    # Retry only archival of this exact response. Never repeat the SDK request.
    for _ in range(3):
        try:
            return bool(_rows(client, "complete_oi_watch_inference", {
                "p_event_id": event_id, "p_invocation_token": token,
                "p_prediction": prediction, "p_error": error,
            }))
        except Exception:
            logger.warning("Watch inference archival failed for %s", event_id)
    return False


def process_watch_inference(client: Any, row: dict) -> str:
    """Atomically claim and invoke one fresh ready-watch exactly once."""
    token = str(uuid.uuid4())
    started = time.monotonic()
    try:
        claimed = _rows(client, "claim_oi_watch_inference", {
            "p_event_id": row["event_id"], "p_invocation_token": token,
        })
    except Exception:
        logger.warning("Watch inference claim failed for %s", row["event_id"])
        return "NONE"
    if not claimed:
        return "NONE"
    owned = claimed[0]
    remaining = _budget(owned, time.monotonic() - started)
    if not remaining:
        _complete_inference(client, row["event_id"], token, None, "dispatch_deadline")
        return "FAILED"
    try:
        prediction = invoke_watch_jev(
            owned["input_state"], timeout=min(5.0, remaining),
            dispatch_deadline=time.monotonic() + remaining,
        )
    except Exception as exc:
        # Error class is sufficient for operations; never persist exception text
        # containing provider headers, request bodies or credentials.
        _complete_inference(client, row["event_id"], token, None, type(exc).__name__)
        return "FAILED"
    return "COMPLETED" if _complete_inference(client, row["event_id"], token, prediction, None) else "UNKNOWN"


def _finish_delivery(client: Any, event_id: str, token: str, status: str, is_cancellation: bool, retry_after: float = 0) -> bool:
    # A lost DB acknowledgment authorizes only this same-token persistence retry.
    for _ in range(3):
        try:
            return bool(_rows(client, "finish_oi_watch_delivery", {
                "p_event_id": event_id, "p_delivery_token": token, "p_status": status,
                "p_is_cancellation": is_cancellation, "p_retry_after_seconds": retry_after,
            }))
        except Exception:
            logger.warning("Watch delivery archival failed for %s", event_id)
    return False


def deliver_watch(client: Any, row: dict, *, webhook_url: str | None = None) -> str:
    """Send the claimed immutable payload once, or archive an ambiguous outcome.

    Only Discord's explicit 429 rejection authorizes a later retry. Accepted
    messages require the wait=true response and a Discord message ID.
    """
    url = webhook_url or WEBHOOK_URL
    if not url:
        return "NONE"
    cancellation = bool(row.get("is_cancellation"))
    token = str(uuid.uuid4())
    claimed = False
    try:
        payload = compose_watch_payload(row)
        # Construct transport before claiming so setup cannot consume freshness.
        with httpx.Client(timeout=5.0, transport=httpx.HTTPTransport(retries=0)) as http:
            started = time.monotonic()
            owned = _rows(client, "begin_oi_watch_delivery", {
                "p_event_id": row["event_id"], "p_delivery_token": token,
                "p_payload": payload, "p_is_cancellation": cancellation,
            })
            if not owned:
                return "NONE"
            claimed = True
            # Pin the transport window to claim start. Payload handling or a
            # suspended thread cannot renew the database-authorized send window.
            deadline = started + min(5.0, _budget(owned[0], 0))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("delivery_deadline")
            payload = owned[0]["cancellation_payload" if cancellation else "delivery_payload"]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("delivery_deadline")
            http.timeout = httpx.Timeout(min(5.0, remaining))
            response = http.post(url, params={"wait": "true"}, json=payload,
                                 headers={"Content-Type": "application/json"})
        status, retry_after = "UNKNOWN", 0.0
        if response.status_code in (200, 201) and response.json().get("id"):
            status = "SENT"
        elif response.status_code == 429:
            retry_after = float(response.json().get("retry_after", response.headers.get("Retry-After", "5")))
            if not math.isfinite(retry_after) or retry_after < 0:
                raise ValueError("Invalid retry-after")
            status = "RETRY"
        return status if _finish_delivery(client, row["event_id"], token, status, cancellation, retry_after) else "UNKNOWN"
    except Exception:
        # Failed/uncertain claim never starts transport. Claimed transport may
        # have reached Discord; never infer rejection from a local exception.
        if claimed:
            _finish_delivery(client, row["event_id"], token, "UNKNOWN", cancellation)
            return "UNKNOWN"
        logger.warning("Watch delivery preparation/claim failed for %s", row["event_id"])
        return "NONE"


def _watch_loop(stop: Event, client_factory: Callable[[], Any], *, inference: bool) -> None:
    client = None
    poll = "poll_oi_watch_inference" if inference else "poll_oi_watch_delivery"
    while not stop.is_set():
        delay = 0.5
        try:
            if client is None:
                client = client_factory()
            rows = _rows(client, poll)
            # Check stop between items. Cancellation recovery still runs after
            # session close; the database gates new watch inference/delivery.
            for row in rows:
                if stop.is_set():
                    break
                if inference:
                    process_watch_inference(client, row)
                else:
                    deliver_watch(client, row)
        except Exception:
            # Missing migration/temporary DB failure affects this worker only.
            logger.warning("Watch %s unavailable; retrying later", "inference" if inference else "delivery")
            delay = 30
        stop.wait(delay)


def start_watch_workers(stop: Event, client_factory: Callable[[], Any]) -> list[Thread]:
    """Start independent watch loops; caller owns shutdown through stop."""
    if not OI_WATCH_JEV_CONFIG.oi_watch_jev_enabled:
        return []
    threads = []
    for inference in (True, False):
        thread = Thread(target=_watch_loop, args=(stop, client_factory),
                        kwargs={"inference": inference},
                        name="jev-watch-inference" if inference else "jev-watch-delivery", daemon=True)
        thread.start()
        threads.append(thread)
    return threads
