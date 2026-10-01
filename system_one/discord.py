"""system_one.discord — Durable Discord delivery for Jev follow-up alerts.

Sends a separate, identifiable follow-up message after successful Jev
inference persistence. Implements the durable delivery contract:
PENDING → SENDING → SENT | DELIVERY_UNKNOWN | DELIVERY_FAILED | SUPPRESSED_EXPIRED.

This module must NOT import from alerts.py, main.py, ml_signal,
SignalPredictor, or storage.py.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

import httpx
from supabase import Client as SupabaseClient

logger = logging.getLogger("system_one.discord")

IST = timezone(timedelta(hours=5, minutes=30))

# Webhook URL for Jev follow-up alerts - Uses the exact same channel as main ARES alerts
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")


def _check_freshness(
    supabase: SupabaseClient,
    signal_uuid: str,
    job_id: int,
) -> Tuple[bool, Optional[str]]:
    """Recheck event-time eligibility against database time.

    Returns (is_fresh, reason) where reason explains rejection.
    Must be called immediately before EVERY delivery attempt.
    """
    try:
        result = supabase.rpc("check_jev_alert_freshness", {
            "p_signal_uuid": signal_uuid,
            "p_job_id": job_id,
        }).execute()
        data = result.data
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if isinstance(data, dict):
            return data.get("is_fresh", False), data.get("reason")
        # Fallback: query job directly
        return _check_freshness_fallback(supabase, signal_uuid)
    except Exception as exc:
        logger.warning("Freshness check failed for %s: %s", signal_uuid, exc)
        return False, f"database_error: {exc}"


def _check_freshness_fallback(
    supabase: SupabaseClient,
    signal_uuid: str,
) -> Tuple[bool, Optional[str]]:
    """Fallback freshness check using direct query when RPC unavailable."""
    try:
        job = supabase.table("llm_prediction_jobs").select(
            "event_at, expires_at, status, alert_status"
        ).eq("signal_uuid", signal_uuid).single().execute()
        row = job.data
        if not row:
            return False, "job_not_found"

        # Terminal states
        if row["status"] in ("FAILED", "UNKNOWN", "EXPIRED"):
            return False, f"job_status_{row['status']}"
        if row["alert_status"] in ("SENT", "DELIVERY_UNKNOWN", "DELIVERY_FAILED", "SUPPRESSED_EXPIRED"):
            return False, f"alert_terminal_{row['alert_status']}"

        # Expiry check: use database now() via a lightweight query
        now_result = supabase.rpc("now").execute()
        # now_result.data is the timestamp string
        db_now_str = now_result.data
        if isinstance(db_now_str, list):
            db_now_str = db_now_str[0] if db_now_str else None
        if db_now_str is None:
            return False, "cannot_determine_db_time"

        db_now = datetime.fromisoformat(str(db_now_str).replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))

        if db_now >= expires_at:
            return False, "expired"

        return True, None
    except Exception as exc:
        return False, f"fallback_error: {exc}"


def _transition_to_sending(
    supabase: SupabaseClient,
    signal_uuid: str,
    payload: Dict[str, Any],
) -> Optional[str]:
    """Atomically transition alert_status from PENDING/RETRYABLE to SENDING.

    Returns the attempt_token on success, None if transition fails.
    """
    attempt_token = str(uuid.uuid4())
    attempt_start = datetime.now(timezone.utc).isoformat()

    try:
        # Atomic update: only transitions eligible states
        result = supabase.table("llm_prediction_jobs").update({
            "alert_status": "SENDING",
            "alert_attempt_token": attempt_token,
            "alert_owner": f"jev-{os.getpid()}",
            "alert_attempt_started_at": attempt_start,
            "updated_at": attempt_start,
        }).eq(
            "signal_uuid", signal_uuid
        ).in_(
            "alert_status", ["PENDING", "RETRYABLE"]
        ).execute()

        if not result.data:
            logger.info("Failed to transition %s to SENDING (not eligible)", signal_uuid)
            return None
        return attempt_token
    except Exception as exc:
        logger.warning("Transition to SENDING failed for %s: %s", signal_uuid, exc)
        return None


def format_jev_followup(
    signal_display_id: str,
    setup_type: str,
    direction: str,
    t1_prob: float,
    t2_prob: float,
    sl_prob: float,
    regime: str,
    regime_confidence: float,
    setup_quality: float,
    is_trap_prob: float,
    engine_name: str,
) -> Dict[str, Any]:
    """Format the Discord embed payload for a Jev follow-up alert."""
    is_bullish = direction == "BULLISH"
    color = 3447003 if is_bullish else 15105570  # Blue-ish / Orange-ish (distinct from main alert)
    icon = "🧠 🐂" if is_bullish else "🧠 🐻"

    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist).strftime("%d-%b-%Y %H:%M:%S")

    neither_prob = max(0.0, 1.0 - t1_prob - sl_prob)

    fields = [
        {"name": "🎯 T1 Hit", "value": f"**{t1_prob*100:.1f}%**", "inline": True},
        {"name": "🎯 T2 Hit", "value": f"**{t2_prob*100:.1f}%**", "inline": True},
        {"name": "🛑 SL Hit", "value": f"**{sl_prob*100:.1f}%**", "inline": True},
        {"name": "⏳ Neither", "value": f"**{neither_prob*100:.1f}%**", "inline": True},
        {"name": "📊 Regime", "value": f"**{regime}** (conf: {regime_confidence:.2f})", "inline": True},
        {"name": "⭐ Quality", "value": f"**{setup_quality:.1f}/10**", "inline": True},
        {"name": "⚠️ Trap Risk", "value": f"**{is_trap_prob*100:.1f}%**", "inline": True},
        {"name": "🤖 Engine", "value": f"`{engine_name}`", "inline": True},
        {"name": "🕒 Time", "value": f"{now_ist} IST", "inline": False},
    ]

    return {
        "embeds": [{
            "title": f"{icon} #{signal_display_id} JEV FORWARD PREDICTION: {setup_type} ({direction})",
            "color": color,
            "fields": fields,
            "footer": {
                "text": "System One • Shadow estimate (uncalibrated) • Does not affect trading"
            },
        }],
    }


def send_jev_followup(
    supabase: SupabaseClient,
    signal_uuid: str,
    signal_display_id: str,
    setup_type: str,
    direction: str,
    t1_prob: float,
    t2_prob: float,
    sl_prob: float,
    regime: str,
    regime_confidence: float,
    setup_quality: float,
    is_trap_prob: float,
    engine_name: str,
    job_id: int,
    webhook_url: Optional[str] = None,
) -> str:
    """Send a Jev follow-up alert to Discord with durable delivery semantics.

    Returns the final alert_status after the attempt.
    """
    url = webhook_url or WEBHOOK_URL
    if not url:
        logger.warning("No Discord webhook URL configured for Jev alerts")
        return "NONE"

    # Step 1: Fresh eligibility check
    is_fresh, reason = _check_freshness(supabase, signal_uuid, job_id)
    if not is_fresh:
        logger.info("Alert suppressed for %s: %s", signal_uuid, reason)
        _mark_suppressed(supabase, signal_uuid, reason)
        return "SUPPRESSED_EXPIRED"

    # Step 2: Format payload
    payload = format_jev_followup(
        signal_display_id=signal_display_id,
        setup_type=setup_type,
        direction=direction,
        t1_prob=t1_prob,
        t2_prob=t2_prob,
        sl_prob=sl_prob,
        regime=regime,
        regime_confidence=regime_confidence,
        setup_quality=setup_quality,
        is_trap_prob=is_trap_prob,
        engine_name=engine_name,
    )

    # Step 3: Atomic transition to SENDING
    attempt_token = _transition_to_sending(supabase, signal_uuid, payload)
    if attempt_token is None:
        return "NONE"  # Another worker owns it or not eligible

    # Step 4: Final freshness recheck before transport
    is_fresh, reason = _check_freshness(supabase, signal_uuid, job_id)
    if not is_fresh:
        logger.info("Alert suppressed after SENDING transition for %s: %s", signal_uuid, reason)
        _mark_suppressed(supabase, signal_uuid, reason)
        return "SUPPRESSED_EXPIRED"

    # Step 5: Send webhook with wait=true, no hidden retries
    try:
        with httpx.Client(timeout=10.0) as http_client:
            response = http_client.post(
                f"{url}?wait=true",
                json=payload,
                headers={"Content-Type": "application/json"},
            )

        if response.status_code in (200, 201, 204):
            # Success: extract message ID
            msg_data = response.json() if response.content else {}
            message_id = msg_data.get("id")
            _mark_sent(supabase, signal_uuid, attempt_token, message_id)
            return "SENT"

        elif response.status_code == 429:
            # Rate limited: documented rejection, retryable
            retry_after = response.headers.get("Retry-After", "5")
            _mark_retryable(supabase, signal_uuid, attempt_token,
                          f"rate_limited: retry_after={retry_after}",
                          float(retry_after))
            return "RETRYABLE"

        else:
            # Non-retryable failure
            _mark_failed(supabase, signal_uuid, attempt_token,
                        f"http_{response.status_code}: {response.text[:200]}")
            return "DELIVERY_FAILED"

    except httpx.TimeoutException:
        _mark_unknown(supabase, signal_uuid, attempt_token, "timeout")
        return "DELIVERY_UNKNOWN"
    except Exception as exc:
        _mark_unknown(supabase, signal_uuid, attempt_token, str(exc)[:200])
        return "DELIVERY_UNKNOWN"


def _mark_sent(supabase: SupabaseClient, signal_uuid: str, token: str, message_id: Optional[str]) -> None:
    try:
        supabase.table("llm_prediction_jobs").update({
            "alert_status": "SENT",
            "alert_discord_message_id": message_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("signal_uuid", signal_uuid).eq("alert_attempt_token", token).execute()
    except Exception as exc:
        logger.error("Failed to mark SENT for %s: %s", signal_uuid, exc)


def _mark_retryable(supabase: SupabaseClient, signal_uuid: str, token: str, reason: str, backoff_s: float) -> None:
    try:
        backoff_until = datetime.now(timezone.utc) + timedelta(seconds=backoff_s)
        supabase.table("llm_prediction_jobs").update({
            "alert_status": "RETRYABLE",
            "alert_rejection_reason": reason,
            "alert_backoff_until": backoff_until.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("signal_uuid", signal_uuid).eq("alert_attempt_token", token).execute()
    except Exception as exc:
        logger.error("Failed to mark RETRYABLE for %s: %s", signal_uuid, exc)


def _mark_failed(supabase: SupabaseClient, signal_uuid: str, token: str, reason: str) -> None:
    try:
        supabase.table("llm_prediction_jobs").update({
            "alert_status": "DELIVERY_FAILED",
            "alert_rejection_reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("signal_uuid", signal_uuid).eq("alert_attempt_token", token).execute()
    except Exception as exc:
        logger.error("Failed to mark DELIVERY_FAILED for %s: %s", signal_uuid, exc)


def _mark_unknown(supabase: SupabaseClient, signal_uuid: str, token: str, reason: str) -> None:
    try:
        supabase.table("llm_prediction_jobs").update({
            "alert_status": "DELIVERY_UNKNOWN",
            "alert_rejection_reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("signal_uuid", signal_uuid).eq("alert_attempt_token", token).execute()
    except Exception as exc:
        logger.error("Failed to mark DELIVERY_UNKNOWN for %s: %s", signal_uuid, exc)


def _mark_suppressed(supabase: SupabaseClient, signal_uuid: str, reason: Optional[str]) -> None:
    try:
        supabase.table("llm_prediction_jobs").update({
            "alert_status": "SUPPRESSED_EXPIRED",
            "alert_rejection_reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("signal_uuid", signal_uuid).in_(
            "alert_status", ["PENDING", "RETRYABLE", "NONE"]
        ).execute()
    except Exception as exc:
        logger.error("Failed to mark SUPPRESSED_EXPIRED for %s: %s", signal_uuid, exc)
