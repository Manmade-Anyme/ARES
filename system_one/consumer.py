"""system_one.consumer — Jev prediction consumer process.

Runs as a separate Fly process group (python -m system_one.consumer).
Polls for signal-bound ml_collection rows, builds context, invokes Jev,
persists results, and sends Discord follow-up alerts.

This module must NOT import from main.py, ml_signal, SignalPredictor,
storage.py, or alerts.py.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from supabase import create_client, Client as SupabaseClient

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
    }, on_conflict="consumer_id").execute()

    if not insert_result.data:
        raise RuntimeError("Failed to bootstrap consumer state")

    row = insert_result.data[0]
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
def _poll_eligible_signals(
    supabase: SupabaseClient,
    live_from: str,
    max_age_seconds: int,
) -> List[Dict[str, Any]]:
    """Poll for signal-bound ml_collection rows eligible for Jev prediction.

    Eligibility requires:
    - signal_uuid is not null (signal-bound snapshot)
    - timestamp >= live_from (post-rollout)
    - timestamp + max_age_seconds > database now() (not expired)
    - No existing llm_prediction_jobs row for this signal_uuid
    """
    try:
        # Query signal-bound snapshots that don't have jobs yet
        result = supabase.table("ml_collection").select(
            "snapshot_uuid, signal_uuid, timestamp, spot, "
            "candle_features, volume_features, iv_features, oi_features, "
            "greek_features, structure_features, meta_features, "
            "raw_candle, raw_atm_oi, oi_wall_context"
        ).not_.is_("signal_uuid", "null").gte(
            "timestamp", live_from
        ).order(
            "timestamp", desc=True
        ).limit(10).execute()

        if not result.data:
            return []

        # Filter out signals that already have jobs
        signal_uuids = [r["signal_uuid"] for r in result.data if r.get("signal_uuid")]
        if not signal_uuids:
            return []

        existing = supabase.table("llm_prediction_jobs").select(
            "signal_uuid"
        ).in_("signal_uuid", signal_uuids).execute()

        claimed = {r["signal_uuid"] for r in (existing.data or [])}
        eligible = [r for r in result.data if r["signal_uuid"] not in claimed]

        return eligible
    except Exception as exc:
        logger.warning("Signal poll failed: %s", exc)
        return []


def _fetch_signal_row(
    supabase: SupabaseClient,
    signal_uuid: str,
) -> Optional[Dict[str, Any]]:
    """Fetch the ares_signals row by signal_uuid."""
    try:
        result = supabase.table("ares_signals").select(
            "signal_uuid, setup_type, direction, trigger_price, spot_at_signal, "
            "stop_loss, target_1, target_2, reasons, timestamp, confidence, display_id"
        ).eq("signal_uuid", signal_uuid).execute()
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
) -> Optional[str]:
    """Atomically transition CLAIMED → INVOKING. Returns invocation_token."""
    invocation_token = str(uuid.uuid4())
    invocation_started = datetime.now(timezone.utc).isoformat()
    try:
        result = supabase.table("llm_prediction_jobs").update({
            "status": "INVOKING",
            "invocation_started_at": invocation_started,
            "invocation_token": invocation_token,
            "updated_at": invocation_started,
        }).eq(
            "signal_uuid", signal_uuid
        ).eq(
            "owner_token", owner_token
        ).eq(
            "status", "CLAIMED"
        ).execute()

        if not result.data:
            logger.warning("Failed to transition %s to INVOKING", signal_uuid)
            return None
        return invocation_token
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
    latency_snapshot_read_ms: float,
    latency_context_build_ms: float,
    latency_total_ms: float,
) -> Optional[int]:
    """Persist the Jev prediction to llm_predictions. Returns prediction id."""
    response_received = datetime.now(timezone.utc).isoformat()
    try:
        result = supabase.table("llm_predictions").insert({
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
            "latency_context_build_ms": latency_context_build_ms,
            "latency_typesafe_ms": jev_result.latency_ms,
            "latency_total_ms": latency_total_ms,
            "signal_timestamp": signal_timestamp,
            "invocation_started_at": invocation_started_at,
            "response_received_at": response_received,
        }).execute()
        if result.data:
            return result.data[0].get("id")
        return None
    except Exception as exc:
        logger.error("Failed to persist prediction for %s: %s", signal_uuid, exc)
        return None


def _complete_job(
    supabase: SupabaseClient,
    signal_uuid: str,
    invocation_token: str,
    prediction_id: Optional[int],
) -> None:
    """Mark job COMPLETED and set alert_status to PENDING."""
    try:
        supabase.table("llm_prediction_jobs").update({
            "status": "COMPLETED",
            "prediction_id": prediction_id,
            "alert_status": "PENDING",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq(
            "signal_uuid", signal_uuid
        ).eq(
            "invocation_token", invocation_token
        ).execute()
    except Exception as exc:
        logger.error("Failed to complete job for %s: %s", signal_uuid, exc)


def _fail_job(supabase: SupabaseClient, signal_uuid: str, reason: str, status: str = "FAILED") -> None:
    """Mark job as FAILED or UNKNOWN."""
    try:
        supabase.table("llm_prediction_jobs").update({
            "status": status,
            "error_message": reason[:500],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("signal_uuid", signal_uuid).execute()
    except Exception as exc:
        logger.error("Failed to mark job %s for %s: %s", status, signal_uuid, exc)


def _expire_stale_jobs(supabase: SupabaseClient) -> None:
    """Mark expired pending jobs as EXPIRED."""
    try:
        # Jobs where event_at + max_age has passed and status is still CLAIMED
        supabase.table("llm_prediction_jobs").update({
            "status": "EXPIRED",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("status", "CLAIMED").lt(
            "expires_at", datetime.now(timezone.utc).isoformat()
        ).is_("invocation_started_at", "null").execute()
    except Exception as exc:
        logger.debug("Expire stale jobs failed: %s", exc)


# ---------------------------------------------------------------------------
# Process one signal
# ---------------------------------------------------------------------------
def process_signal(
    supabase: SupabaseClient,
    snapshot_row: Dict[str, Any],
) -> Optional[str]:
    """Process a single signal: claim → build context → invoke Jev → persist → alert.

    Returns status string or None.
    """
    signal_uuid = snapshot_row["signal_uuid"]
    event_at = snapshot_row["timestamp"]
    snapshot_uuid = snapshot_row.get("snapshot_uuid")
    total_start = time.monotonic()

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

    # Transition to INVOKING
    invocation_token = _transition_to_invoking(supabase, signal_uuid, owner_token)
    if not invocation_token:
        return None

    invocation_started = datetime.now(timezone.utc).isoformat()

    # Build context
    ctx_start = time.monotonic()
    try:
        xgboost_row = _fetch_xgboost_row(supabase, signal_uuid)
        context = build_context(signal_row, snapshot_row, xgboost_row)
    except (BarrierValidationError, ValueError) as exc:
        _fail_job(supabase, signal_uuid, f"context_error: {exc}")
        logger.warning("Context build failed for %s: %s", signal_uuid, exc)
        return "FAILED"
    latency_ctx_ms = (time.monotonic() - ctx_start) * 1000

    # Invoke Jev
    try:
        jev_result = invoke_jev(context)
    except Exception as exc:
        error_type = type(exc).__name__
        _fail_job(supabase, signal_uuid, f"{error_type}: {exc}", status="UNKNOWN")
        logger.warning("Jev invocation failed for %s: %s: %s", signal_uuid, error_type, exc)
        return "UNKNOWN"

    latency_total_ms = (time.monotonic() - total_start) * 1000

    # Persist prediction
    prediction_id = _persist_prediction(
        supabase, signal_uuid, jev_result, context,
        snapshot_uuid, event_at, invocation_started,
        latency_read_ms, latency_ctx_ms, latency_total_ms,
    )

    # Complete job
    _complete_job(supabase, signal_uuid, invocation_token, prediction_id)

    # Send Discord follow-up
    try:
        alert_status = send_jev_followup(
            supabase=supabase,
            signal_uuid=signal_uuid,
            signal_display_id=signal_row.get("display_id", signal_uuid[:8]),
            setup_type=signal_row.get("setup_type", "UNKNOWN"),
            direction=signal_row.get("direction", "UNKNOWN"),
            t1_prob=jev_result.t1_hit_prob,
            t2_prob=jev_result.t2_hit_prob,
            sl_prob=jev_result.sl_hit_prob,
            regime=jev_result.regime,
            regime_confidence=jev_result.regime_confidence,
            setup_quality=jev_result.setup_quality,
            is_trap_prob=jev_result.is_trap_prob,
            engine_name=jev_result.engine_name,
            job_id=job.get("id", 0),
        )
        logger.info(
            "Processed %s: T1=%.1f%% SL=%.1f%% regime=%s quality=%.1f alert=%s latency=%.0fms",
            signal_uuid[:8], jev_result.t1_hit_prob * 100, jev_result.sl_hit_prob * 100,
            jev_result.regime, jev_result.setup_quality, alert_status, latency_total_ms,
        )
    except Exception as exc:
        logger.warning("Discord follow-up failed for %s: %s", signal_uuid, exc)

    return "COMPLETED"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
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

    while True:
        try:
            if not _is_trading_session():
                # Outside trading hours: check less frequently
                now_ist = datetime.now(IST)
                if now_ist.hour >= SESSION_END_HOUR and now_ist.minute >= SESSION_END_MINUTE:
                    logger.info("Session ended. Shutting down.")
                    break
                time.sleep(30)
                continue

            # Expire stale jobs
            _expire_stale_jobs(supabase)

            # Poll for eligible signals
            eligible = _poll_eligible_signals(supabase, live_from, max_age)

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


if __name__ == "__main__":
    run()
