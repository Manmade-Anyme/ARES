"""system_one.discord — Durable Discord delivery for Jev follow-up alerts.

Sends a separate, identifiable follow-up message after successful Jev
inference persistence. Implements the durable delivery contract:
PENDING → SENDING → SENT | DELIVERY_UNKNOWN | DELIVERY_FAILED | SUPPRESSED_EXPIRED.

This module must NOT import from alerts.py, main.py, ml_signal,
SignalPredictor, or storage.py.
"""

from __future__ import annotations

import logging
import math
import os
import time
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
    attempt_token: Optional[str] = None,
) -> Tuple[bool, Optional[str], float]:
    """Recheck event-time eligibility against database time.

    Returns (is_fresh, reason, remaining_seconds) from the database gate.
    Must be called immediately before EVERY delivery attempt.
    """
    try:
        result = supabase.rpc("check_jev_alert_freshness", {
            "p_signal_uuid": signal_uuid,
            "p_job_id": job_id,
            "p_attempt_token": attempt_token,
        }).execute()
        data = result.data
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if isinstance(data, dict):
            remaining = float(data.get("remaining_seconds", 0))
            if data.get("is_fresh") is True and math.isfinite(remaining) and remaining > 0:
                return True, None, remaining
            return False, data.get("reason") or "invalid_gate_response", 0
        return False, "invalid_gate_response", 0
    except Exception as exc:
        logger.warning("Freshness check failed for %s: %s", signal_uuid, exc)
        return False, "database_error", 0


def _transition_to_sending(
    supabase: SupabaseClient,
    signal_uuid: str,
    payload: Dict[str, Any],
    job_id: int,
    destination: str,
) -> Optional[Dict[str, Any]]:
    """Atomically transition alert_status from PENDING/RETRYABLE to SENDING.

    Returns the pinned delivery record after an acknowledged database transition.
    """
    attempt_token = str(uuid.uuid4())
    try:
        result = supabase.rpc("begin_jev_alert_attempt", {
            "p_signal_uuid": signal_uuid,
            "p_job_id": job_id,
            "p_attempt_token": attempt_token,
            "p_owner": f"jev-{os.getpid()}-{uuid.uuid4()}",
            "p_payload": payload,
            "p_destination": destination,
        }).execute()
        if not result.data:
            logger.info("Failed to transition %s to SENDING (not eligible)", signal_uuid)
            return None
        return result.data[0] if isinstance(result.data, list) else result.data
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
    is_fresh, reason, _ = _check_freshness(supabase, signal_uuid, job_id)
    if not is_fresh:
        logger.info("Alert suppressed for %s: %s", signal_uuid, reason)
        if reason in ("expired", "session_closed"):
            _mark_suppressed(supabase, signal_uuid, reason)
            return "SUPPRESSED_EXPIRED"
        return "NONE"

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
    attempt = _transition_to_sending(supabase, signal_uuid, payload, job_id, url)
    if attempt is None:
        return "NONE"  # Another worker owns it or not eligible
    attempt_token = attempt["alert_attempt_token"]
    payload = attempt["alert_payload"]
    url = attempt["alert_destination"]

    # Initialize transport before the final gate so setup cannot consume freshness.
    try:
        with httpx.Client(timeout=10.0, transport=httpx.HTTPTransport(retries=0)) as http_client:
            gate_start = time.monotonic()
            is_fresh, reason, remaining = _check_freshness(supabase, signal_uuid, job_id, attempt_token)
            remaining -= time.monotonic() - gate_start
            if is_fresh and remaining <= 0:
                is_fresh, reason = False, "expired"
            if not is_fresh:
                # This owner has not started transport. Never overwrite another attempt.
                if reason in ("expired", "session_closed"):
                    _mark_suppressed(supabase, signal_uuid, reason, attempt_token)
                    return "SUPPRESSED_EXPIRED"
                _mark_failed(supabase, signal_uuid, attempt_token, reason or "gate_failed")
                return "DELIVERY_FAILED"
            http_client.timeout = httpx.Timeout(min(10.0, remaining))
            response = http_client.post(
                url,
                params={"wait": "true"},
                json=payload,
                headers={"Content-Type": "application/json"},
            )

        if response.status_code in (200, 201):
            # Success: extract message ID
            msg_data = response.json() if response.content else {}
            message_id = msg_data.get("id")
            if not message_id:
                _mark_unknown(supabase, signal_uuid, attempt_token, "missing_message_confirmation")
                return "DELIVERY_UNKNOWN"
            return "SENT" if _mark_sent(supabase, signal_uuid, attempt_token, message_id) else "DELIVERY_UNKNOWN"

        elif response.status_code == 429:
            # Rate limited: documented rejection, retryable
            retry_after = response.json().get("retry_after", response.headers.get("Retry-After", "5"))
            backoff = float(retry_after)
            if not math.isfinite(backoff) or backoff < 0:
                raise ValueError("Invalid Discord retry_after")
            _mark_retryable(supabase, signal_uuid, attempt_token,
                          f"rate_limited: retry_after={retry_after}",
                          backoff)
            return "RETRYABLE"

        elif response.status_code in (400, 401, 403, 404, 405, 413, 422):
            # Non-retryable failure
            _mark_failed(supabase, signal_uuid, attempt_token,
                        f"http_{response.status_code}: {response.text[:200]}")
            return "DELIVERY_FAILED"
        else:
            _mark_unknown(supabase, signal_uuid, attempt_token, f"ambiguous_http_{response.status_code}")
            return "DELIVERY_UNKNOWN"

    except httpx.TimeoutException:
        _mark_unknown(supabase, signal_uuid, attempt_token, "timeout")
        return "DELIVERY_UNKNOWN"
    except Exception as exc:
        _mark_unknown(supabase, signal_uuid, attempt_token, str(exc)[:200])
        return "DELIVERY_UNKNOWN"


def _mark_sent(supabase: SupabaseClient, signal_uuid: str, token: str, message_id: str) -> bool:
    # Retry only persistence of the same acknowledgment, never external delivery.
    for _ in range(3):
        try:
            result = supabase.table("llm_prediction_jobs").update({
                "alert_status": "SENT",
                "alert_discord_message_id": message_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("signal_uuid", signal_uuid).eq("alert_attempt_token", token).in_(
                "alert_status", ["SENDING", "DELIVERY_UNKNOWN", "SENT"]
            ).execute()
            return bool(result.data)
        except Exception as exc:
            logger.error("Failed to persist SENT for %s: %s", signal_uuid, exc)
    return False


def _mark_retryable(supabase: SupabaseClient, signal_uuid: str, token: str, reason: str, backoff_s: float) -> None:
    try:
        supabase.rpc("mark_jev_alert_retryable", {
            "p_signal_uuid": signal_uuid,
            "p_attempt_token": token,
            "p_reason": reason,
            "p_backoff_seconds": backoff_s,
        }).execute()
    except Exception as exc:
        logger.error("Failed to mark RETRYABLE for %s: %s", signal_uuid, exc)


def _mark_failed(supabase: SupabaseClient, signal_uuid: str, token: str, reason: str) -> None:
    try:
        supabase.table("llm_prediction_jobs").update({
            "alert_status": "DELIVERY_FAILED",
            "alert_rejection_reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("signal_uuid", signal_uuid).eq("alert_attempt_token", token).eq("alert_status", "SENDING").execute()
    except Exception as exc:
        logger.error("Failed to mark DELIVERY_FAILED for %s: %s", signal_uuid, exc)


def _mark_unknown(supabase: SupabaseClient, signal_uuid: str, token: str, reason: str) -> None:
    try:
        supabase.table("llm_prediction_jobs").update({
            "alert_status": "DELIVERY_UNKNOWN",
            "alert_rejection_reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("signal_uuid", signal_uuid).eq("alert_attempt_token", token).eq("alert_status", "SENDING").execute()
    except Exception as exc:
        logger.error("Failed to mark DELIVERY_UNKNOWN for %s: %s", signal_uuid, exc)


def _mark_suppressed(supabase: SupabaseClient, signal_uuid: str, reason: Optional[str], token: Optional[str] = None) -> None:
    try:
        query = supabase.table("llm_prediction_jobs").update({
            "alert_status": "SUPPRESSED_EXPIRED",
            "alert_rejection_reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("signal_uuid", signal_uuid)
        if token:
            query = query.eq("alert_attempt_token", token).eq("alert_status", "SENDING")
        else:
            query = query.in_("alert_status", ["PENDING", "RETRYABLE"])
        query.execute()
    except Exception as exc:
        logger.error("Failed to mark SUPPRESSED_EXPIRED for %s: %s", signal_uuid, exc)
