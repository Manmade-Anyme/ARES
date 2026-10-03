"""system_one.consumer — Jev prediction consumer process.

Runs as a separate Fly process group (python -m system_one.consumer).
Polls for signal-bound ml_collection rows, builds context, invokes Jev,
persists results, and sends Discord follow-up alerts.

This module must NOT import from main.py, ml_signal, SignalPredictor,
storage.py, or alerts.py.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from threading import Event, Thread
from typing import Any, Dict, List, Optional, Tuple

from supabase import create_client, Client as SupabaseClient
from typesafe_sdk import TypeSafeError, TypeSafeAPIError, TypeSafeAPIConnectionError

from . import CONTEXT_VERSION, QUESTION_VERSION
from .context import build_context, BarrierValidationError
from .jev import invoke_jev, JevResult, DEFAULT_MODEL
from .discord import send_jev_followup

logger = logging.getLogger("system_one.consumer")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONSUMER_ID = os.environ.get("JEV_CONSUMER_ID", "jev-primary")
POLL_INTERVAL = float(os.environ.get("JEV_POLL_INTERVAL", "1.0"))
LEASE_SECONDS = int(os.environ.get("JEV_LEASE_SECONDS", "30"))
MAX_SIGNAL_AGE = int(os.environ.get("JEV_MAX_SIGNAL_AGE", "60"))

# Session hours (IST)
SESSION_START_HOUR = 9
SESSION_START_MINUTE = 15
SESSION_END_HOUR = 15
SESSION_END_MINUTE = 30

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------
def _create_supabase_client() -> SupabaseClient:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
    return create_client(url, key)


# ---------------------------------------------------------------------------
# Rollout state
# ---------------------------------------------------------------------------
def _bootstrap_consumer_state(supabase: SupabaseClient) -> Dict[str, Any]:
    """Bootstrap llm_consumer_state row on first start.

    Uses INSERT ... ON CONFLICT DO NOTHING to never overwrite an existing row.
    Returns the persisted row.
    """
    # Try to read existing
    result = supabase.table("llm_consumer_state").select("*").eq(
        "consumer_id", CONSUMER_ID
    ).execute()
    if result.data:
        logger.info("Loaded existing consumer state: live_from=%s", result.data[0]["live_from"])
        return result.data[0]

    # Bootstrap new row
    logger.info("Bootstrapping consumer state for %s", CONSUMER_ID)
    insert_result = supabase.table("llm_consumer_state").upsert({
        "consumer_id": CONSUMER_ID,
        "max_signal_age_seconds": MAX_SIGNAL_AGE,
    }, on_conflict="consumer_id", ignore_duplicates=True).execute()

    # A concurrent bootstrap may win. Load the winner without resetting its cutoff.
    rows = insert_result.data or supabase.table("llm_consumer_state").select("*").eq(
        "consumer_id", CONSUMER_ID
    ).execute().data
    if not rows:
        raise RuntimeError("Failed to bootstrap consumer state")

    row = rows[0]
    logger.info("Bootstrapped consumer state: live_from=%s", row["live_from"])
    return row


# ---------------------------------------------------------------------------
# Session check
# ---------------------------------------------------------------------------
def _is_trading_session() -> bool:
    now = datetime.now(IST)
    session_start = now.replace(hour=SESSION_START_HOUR, minute=SESSION_START_MINUTE, second=0, microsecond=0)
    session_end = now.replace(hour=SESSION_END_HOUR, minute=SESSION_END_MINUTE, second=0, microsecond=0)
    return session_start <= now < session_end


# ---------------------------------------------------------------------------
# Signal polling
# ---------------------------------------------------------------------------
def _poll_eligible_signals(supabase: SupabaseClient) -> List[Dict[str, Any]]:
    """Read at most ten fresh, unclaimed/recoverable snapshots using database time.

    The RPC loads persisted rollout/age policy and filters expiry/job ownership
    before LIMIT. It normalizes bridge/greenfield UUID columns to signal_uuid.
    The atomic claim still rechecks freshness and ownership before dispatch.
    """
    try:
        read_start = time.monotonic()
        result = supabase.rpc("poll_jev_signals", {
            "p_consumer_id": CONSUMER_ID,
            "p_context_version": CONTEXT_VERSION,
            "p_question_version": QUESTION_VERSION,
            "p_model_name": DEFAULT_MODEL,
        }).execute()
        snapshot_read_ms = (time.monotonic() - read_start) * 1000
        return [dict(row, _jev_snapshot_read_ms=snapshot_read_ms) for row in (result.data or [])]
    except Exception as exc:
        logger.warning("Signal poll failed: %s", exc)
        return []


def _fetch_signal_row(
    supabase: SupabaseClient,
    signal_uuid: str,
) -> Optional[Dict[str, Any]]:
    """Fetch the ares_signals row by signal_uuid."""
    try:
        result = supabase.rpc("read_jev_signal", {"p_signal_uuid": signal_uuid}).execute()
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.warning("Failed to fetch signal %s: %s", signal_uuid, exc)
        return None


def _fetch_xgboost_row(
    supabase: SupabaseClient,
    signal_uuid: str,
) -> Optional[Dict[str, Any]]:
    """Fetch optional ml_predictions row for comparison."""
    try:
        result = supabase.table("ml_predictions").select(
            "probability, confidence_tier, model_version"
        ).eq("signal_id", signal_uuid).limit(1).execute()
        return result.data[0] if result.data else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Claim and invoke
# ---------------------------------------------------------------------------
def _claim_signal(
    supabase: SupabaseClient,
    signal_uuid: str,
    snapshot_uuid: Optional[str],
    event_at: str,
) -> Optional[Dict[str, Any]]:
    """Atomically claim a signal for Jev prediction."""
    owner_token = str(uuid.uuid4())
    try:
        result = supabase.rpc("claim_llm_prediction_job", {
            "p_signal_uuid": signal_uuid,
            "p_owner_token": owner_token,
            "p_lease_seconds": LEASE_SECONDS,
            "p_snapshot_uuid": snapshot_uuid,
            "p_context_version": CONTEXT_VERSION,
            "p_question_version": QUESTION_VERSION,
            "p_model_name": DEFAULT_MODEL,
            "p_event_at": event_at,
            "p_max_age_seconds": MAX_SIGNAL_AGE,
            "p_consumer_id": CONSUMER_ID,
        }).execute()
        if result.data:
            row = result.data[0] if isinstance(result.data, list) else result.data
            return row
        return None
    except Exception as exc:
        if "already has an active" in str(exc):
            logger.debug("Signal %s already claimed", signal_uuid)
        else:
            logger.warning("Claim failed for %s: %s", signal_uuid, exc)
        return None


def _transition_to_invoking(
    supabase: SupabaseClient,
    signal_uuid: str,
    owner_token: str,
) -> Optional[Dict[str, Any]]:
    """Obtain the database-time lease, session, and expiry gate before dispatch."""
    invocation_token = str(uuid.uuid4())
    try:
        result = supabase.rpc("begin_llm_invocation", {
            "p_signal_uuid": signal_uuid,
            "p_owner_token": owner_token,
            "p_invocation_token": invocation_token,
        }).execute()
        if not result.data:
            logger.warning("Failed to transition %s to INVOKING", signal_uuid)
            return None
        return result.data[0] if isinstance(result.data, list) else result.data
    except Exception as exc:
        logger.warning("INVOKING transition failed for %s: %s", signal_uuid, exc)
        return None


def _persist_prediction(
    supabase: SupabaseClient,
    signal_uuid: str,
    jev_result: JevResult,
    context: Dict[str, Any],
    snapshot_uuid: Optional[str],
    signal_timestamp: str,
    invocation_started_at: str,
    invocation_token: str,
    latency_snapshot_read_ms: Optional[float],
    latency_context_build_ms: float,
    latency_signal_read_ms: float,
) -> Optional[int]:
    """Atomically archive the response and complete its job; retry only this response."""
    response_received = datetime.now(timezone.utc).isoformat()
    prediction = {
        "signal_uuid": signal_uuid,
        "t1_hit_prob": jev_result.t1_hit_prob,
        "t2_hit_prob": jev_result.t2_hit_prob,
        "sl_hit_prob": jev_result.sl_hit_prob,
        "regime": jev_result.regime,
        "regime_distribution": jev_result.regime_distribution,
        "regime_confidence": jev_result.regime_confidence,
        "setup_quality": jev_result.setup_quality,
        "is_trap_prob": jev_result.is_trap_prob,
        "engine_name": jev_result.engine_name,
        "context_version": CONTEXT_VERSION,
        "question_version": QUESTION_VERSION,
        "snapshot_uuid": snapshot_uuid,
        "input_state": context,
        "raw_response": jev_result.raw_response,
        "latency_snapshot_read_ms": latency_snapshot_read_ms,
        "latency_signal_read_ms": latency_signal_read_ms,
        "latency_context_build_ms": latency_context_build_ms,
        "latency_typesafe_ms": jev_result.latency_ms,
        "signal_timestamp": signal_timestamp,
        "invocation_started_at": invocation_started_at,
        "response_received_at": response_received,
    }
    for _ in range(3):
        try:
            result = supabase.rpc("complete_llm_prediction_job", {
                "p_signal_uuid": signal_uuid,
                "p_invocation_token": invocation_token,
                "p_prediction": prediction,
            }).execute()
            if result.data:
                row = result.data[0] if isinstance(result.data, list) else result.data
                return row.get("id")
            return None
        except Exception as exc:
            logger.error("Failed to persist prediction for %s: %s", signal_uuid, exc)
    return None


def _record_latency(supabase: SupabaseClient, signal_uuid: str, prediction_id: int,
                    persistence_ms: Optional[float], delivery_ms: Optional[float], confirmed_sent: bool) -> None:
    """Finalize observed stages after persistence/delivery; never repeat a send for metrics."""
    try:
        supabase.rpc("record_llm_prediction_latency", {
            "p_signal_uuid": signal_uuid,
            "p_prediction_id": prediction_id,
            "p_persistence_ms": persistence_ms,
            "p_delivery_ms": delivery_ms,
            "p_confirmed_sent": confirmed_sent,
        }).execute()
    except Exception as exc:
        logger.warning("Latency finalization failed for %s: %s", signal_uuid, exc)


def _fail_job(supabase: SupabaseClient, signal_uuid: str, reason: str,
              owner_token: str, status: str = "FAILED") -> None:
    """Mark job as FAILED or UNKNOWN."""
    try:
        supabase.table("llm_prediction_jobs").update({
            "status": status,
            "error_message": reason[:500],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("signal_uuid", signal_uuid).eq("owner_token", owner_token).in_(
            "status", ["CLAIMED", "INVOKING", "UNKNOWN"]
        ).execute()
    except Exception as exc:
        logger.error("Failed to mark job %s for %s: %s", status, signal_uuid, exc)


def _expire_stale_jobs(supabase: SupabaseClient) -> None:
    """Recover stale inference/send markers using database time, never replaying them."""
    try:
        supabase.rpc("recover_llm_prediction_jobs", {"p_consumer_id": CONSUMER_ID}).execute()
    except Exception as exc:
        logger.debug("Expire stale jobs failed: %s", exc)


# ---------------------------------------------------------------------------
# Process one signal
# ---------------------------------------------------------------------------
def process_signal(
    supabase: SupabaseClient,
    snapshot_row: Dict[str, Any],
) -> Optional[str]:
    """Claim, evaluate and archive one signal with durable PENDING delivery.

    Webhook transport runs independently through the saved-job queue.
    Returns status string or None.
    """
    signal_uuid = snapshot_row["signal_uuid"]
    event_at = snapshot_row["timestamp"]
    snapshot_uuid = snapshot_row.get("snapshot_uuid")

    # Fetch the signal row
    read_start = time.monotonic()
    signal_row = _fetch_signal_row(supabase, signal_uuid)
    if not signal_row:
        logger.warning("No signal row found for %s", signal_uuid)
        return None
    latency_read_ms = (time.monotonic() - read_start) * 1000

    # Claim the signal
    job = _claim_signal(supabase, signal_uuid, snapshot_uuid, event_at)
    if not job:
        return None

    owner_token = job["owner_token"]

    # Build context
    ctx_start = time.monotonic()
    try:
        xgboost_row = _fetch_xgboost_row(supabase, signal_uuid)
        context = build_context(signal_row, snapshot_row, xgboost_row)
    except (BarrierValidationError, ValueError) as exc:
        _fail_job(supabase, signal_uuid, f"context_error: {exc}", owner_token)
        logger.warning("Context build failed for %s: %s", signal_uuid, exc)
        return "FAILED"
    latency_ctx_ms = (time.monotonic() - ctx_start) * 1000

    # Context/database reads may exhaust the age window or lease. Gate last.
    gate_start = time.monotonic()
    invocation = _transition_to_invoking(supabase, signal_uuid, owner_token)
    if not invocation:
        return None
    invocation_token = invocation["invocation_token"]
    invocation_started = invocation["invocation_started_at"]

    # Use a duration from two database timestamps, then subtract the whole RPC
    # round trip conservatively. Worker clock skew must not extend eligibility.
    try:
        dispatch_window = (
            datetime.fromisoformat(invocation["invocation_dispatch_deadline_at"].replace("Z", "+00:00"))
            - datetime.fromisoformat(invocation_started.replace("Z", "+00:00"))
        ).total_seconds()
        dispatch_deadline = gate_start + dispatch_window
        if not math.isfinite(dispatch_window) or time.monotonic() >= dispatch_deadline:
            raise ValueError("invocation dispatch window elapsed")
    except (KeyError, TypeError, ValueError) as exc:
        _fail_job(supabase, signal_uuid, str(exc), owner_token, status="UNKNOWN")
        return "UNKNOWN"

    # Invoke Jev
    try:
        jev_result = invoke_jev(context, dispatch_deadline=dispatch_deadline)
    except TypeSafeError as exc:
        # HTTP 4xx (except request timeout) and local SDK configuration errors
        # prove rejection. Connection failures and 5xx remain ambiguous.
        if isinstance(exc, TypeSafeAPIConnectionError):
            status = "UNKNOWN"
        elif isinstance(exc, TypeSafeAPIError):
            status = "FAILED" if 400 <= exc.status < 500 and exc.status != 408 else "UNKNOWN"
        else:
            status = "FAILED"
        _fail_job(supabase, signal_uuid, f"{type(exc).__name__}: {exc}", owner_token, status=status)
        logger.warning("Jev %s for %s: %s", status, signal_uuid, exc)
        return status
    except Exception as exc:
        error_type = type(exc).__name__
        _fail_job(supabase, signal_uuid, f"{error_type}: {exc}", owner_token, status="UNKNOWN")
        logger.warning("Jev invocation failed for %s: %s: %s", signal_uuid, error_type, exc)
        return "UNKNOWN"

    # Persist prediction
    persist_start = time.monotonic()
    prediction_id = _persist_prediction(
        supabase, signal_uuid, jev_result, context,
        snapshot_uuid, event_at, invocation_started, invocation_token,
        snapshot_row.get("_jev_snapshot_read_ms"), latency_ctx_ms, latency_read_ms,
    )
    persistence_ms = (time.monotonic() - persist_start) * 1000

    if prediction_id is None:
        # Persistence uncertainty must not authorize a webhook or another inference.
        logger.warning("Prediction/job persistence incomplete for %s", signal_uuid)
        return "UNKNOWN"

    # Completion already queued PENDING delivery atomically with the archive.
    # Record persistence now; delivery/total remain unknown until queue processing.
    _record_latency(supabase, signal_uuid, prediction_id, persistence_ms, None, False)
    logger.info(
        "Predicted %s: T1=%.1f%% SL=%.1f%% regime=%s quality=%.1f alert=PENDING",
        signal_uuid[:8], jev_result.t1_hit_prob * 100, jev_result.sl_hit_prob * 100,
        jev_result.regime, jev_result.setup_quality,
    )

    return "COMPLETED"


def _recover_pending_alerts(supabase: SupabaseClient) -> None:
    """Deliver one new pending or proven-rejected alert from its saved prediction."""
    # Database time excludes backoff/expired rows before the bounded batch.
    jobs = supabase.rpc("poll_jev_alert_jobs", {"p_consumer_id": CONSUMER_ID}).execute().data
    # One delivery per pass keeps shutdown/session checks between attempts.
    for job in (jobs or [])[:1]:
        predictions = supabase.table("llm_predictions").select("*").eq(
            "id", job["prediction_id"]
        ).execute().data
        signal = _fetch_signal_row(supabase, job["signal_uuid"])
        if not predictions or not signal:
            continue
        prediction = predictions[0]
        delivery_start = time.monotonic()
        alert_status = "NONE"
        try:
            alert_status = send_jev_followup(
                supabase, job["signal_uuid"], signal.get("display_id", job["signal_uuid"][:8]),
                signal["setup_type"], signal["direction"],
                float(prediction["t1_hit_prob"]), float(prediction["t2_hit_prob"]), float(prediction["sl_hit_prob"]),
                prediction["regime"], float(prediction["regime_confidence"]),
                float(prediction["setup_quality"]), float(prediction["is_trap_prob"]),
                prediction["engine_name"], job["id"],
            )
        except Exception as exc:
            logger.warning("Discord follow-up failed for %s: %s", job["signal_uuid"], exc)

        _record_latency(supabase, job["signal_uuid"], job["prediction_id"], None,
                        (time.monotonic() - delivery_start) * 1000, alert_status == "SENT")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def _delivery_loop(stop: Event) -> None:
    """Drain saved alerts independently, using a client owned by this thread.

    No database/HTTP client is shared with inference. Database send markers,
    not thread scheduling, remain the authority for delivery and retries.
    """
    supabase = None
    while not stop.is_set():
        delay = POLL_INTERVAL
        try:
            if not _is_trading_session():
                delay = 30
            else:
                if supabase is None:
                    supabase = _create_supabase_client()
                _recover_pending_alerts(supabase)
        except Exception as exc:
            logger.error("Delivery loop error: %s", exc)
        stop.wait(delay)


def run() -> None:
    """Main consumer loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("Starting Jev consumer (id=%s, poll=%ss, age=%ss)",
                CONSUMER_ID, POLL_INTERVAL, MAX_SIGNAL_AGE)

    supabase = _create_supabase_client()
    state = _bootstrap_consumer_state(supabase)
    live_from = state["live_from"]
    max_age = state["max_signal_age_seconds"]

    logger.info("Consumer live_from=%s, max_age=%ss", live_from, max_age)

    # A slow webhook must not consume the next snapshot's invocation window.
    # Queue state is durable; never pass predictions through an in-memory queue.
    delivery_stop = Event()
    delivery_thread = Thread(target=_delivery_loop, args=(delivery_stop,),
                             name="jev-delivery", daemon=True)
    delivery_thread.start()
    try:
        while True:
            try:
                if not _is_trading_session():
                    # Keep this Machine ready for the next session without DB/API polling.
                    time.sleep(30)
                    continue

                _expire_stale_jobs(supabase)

                # Poll for eligible signals; delivery never runs on this thread.
                eligible = _poll_eligible_signals(supabase)

                for snapshot in eligible:
                    try:
                        process_signal(supabase, snapshot)
                    except Exception as exc:
                        logger.error("Unhandled error processing %s: %s",
                                   snapshot.get("signal_uuid", "?")[:8], exc)

            except KeyboardInterrupt:
                logger.info("Consumer stopped by user.")
                break
            except Exception as exc:
                logger.error("Consumer loop error: %s", exc)

            time.sleep(POLL_INTERVAL)
    finally:
        delivery_stop.set()
        # Do not wait for a webhook timeout at shutdown. Any interrupted SENDING
        # attempt follows the existing durable UNKNOWN/no-replay recovery rule.
        delivery_thread.join(timeout=1)


if __name__ == "__main__":
    run()
