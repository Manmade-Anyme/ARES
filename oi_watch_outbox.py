"""Application-only ordered persistence of frozen watch observations.

Inference and webhook delivery belong to the Jev worker. A failed or ambiguous
register is retried with the same UUID; it never triggers a second delivery path.
"""
import asyncio
from copy import deepcopy
from datetime import timezone
import logging
import json
import time
from queue import Empty, Queue
from threading import Event, Thread
from uuid import uuid4

from config import settings
from oi_watch_context import build_watch_context, CONTEXT_VERSION

logger = logging.getLogger("ares.oi_watch_outbox")


class OIWatchOutbox:
    """Own one database client and serialize registration before lifecycle updates."""

    def __init__(self, *, client_factory=None):
        self.producer_run_id = str(uuid4())
        self._factory = client_factory
        self._queue = Queue()
        self._ready = Event()
        self._stop = Event()
        self._error = None
        self._thread = Thread(target=self._run, name="oi-watch-outbox", daemon=True)
        self._captured = set()

    async def start(self) -> bool:
        """Check migration/client availability without blocking the trading loop."""
        self._thread.start()
        ready = await asyncio.to_thread(self._ready.wait, 5.0)
        if not ready or self._error is not None:
            self._stop.set()
            logger.warning("OI watch outbox unavailable: %s", self._error or "startup timeout")
            return False
        return True

    def submit_watch(self, observation, base_payload, restart_payload, **market) -> None:
        """Capture once synchronously; enqueue only JSON-safe immutable inputs."""
        if observation.event_id in self._captured:
            return
        try:
            state = build_watch_context(
                observation.bias, observation.spot, observation.timestamp,
                minimum_move_points=settings.oi_watch_jev_minimum_move_points, **market,
            )
            # Validate and freeze preparation outputs before any remote register.
            state = json.loads(json.dumps(deepcopy(state), allow_nan=False))
        except Exception as exc:
            # The base embed is already a pure JSON payload. Invalid evidence
            # must produce the same watch with an unavailable assessment, not
            # drop the observation or invoke inference with fabricated inputs.
            state = {
                "context_version": CONTEXT_VERSION,
                "observed_at": observation.timestamp.isoformat(),
                "context_preparation_error": type(exc).__name__,
            }
        params = {
            "p_event_id": observation.event_id,
            "p_producer_run_id": self.producer_run_id,
            "p_observed_at": observation.timestamp.astimezone(timezone.utc).isoformat(),
            "p_input_state": state,
            "p_base_payload": base_payload,
            "p_restart_cancellation_payload": restart_payload,
            "p_wait_seconds": settings.oi_watch_jev_wait_seconds,
            "p_max_age_seconds": settings.oi_watch_jev_max_age_seconds,
        }
        self._queue.put(("enqueue_oi_watch", json.loads(json.dumps(params, allow_nan=False))))
        self._captured.add(observation.event_id)

    def submit_transition(self, event_id, action, cancellation_payload=None) -> None:
        """Queue a terminal update after all prior registration attempts."""
        self._queue.put(("transition_oi_watch", deepcopy({
            "p_event_id": event_id, "p_action": action,
            "p_cancellation_payload": cancellation_payload,
        })))

    async def flush(self, timeout=5.0) -> bool:
        """Wait a bounded time for background writes at lifecycle boundaries."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._queue.unfinished_tasks and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        return self._queue.unfinished_tasks == 0

    async def wait_for_delivery(self, event_id, timeout=6.0) -> bool:
        """Order a read barrier after writes; wait only for a known in-flight post."""
        complete = Event()
        request = {"event_id": event_id, "deadline": time.monotonic() + timeout,
                   "complete": complete, "settled": False}
        self._queue.put(("__wait_delivery__", request))
        ready = await asyncio.to_thread(complete.wait, timeout)
        return ready and request["settled"]

    def _read_delivery_barrier(self, client, request) -> None:
        try:
            while not self._stop.is_set() and time.monotonic() < request["deadline"]:
                rows = client.table("oi_watch_predictions").select("delivery_status").eq(
                    "event_id", request["event_id"],
                ).execute().data
                if not rows or rows[0].get("delivery_status") != "SENDING":
                    request["settled"] = True
                    break
                self._stop.wait(min(0.05, max(0, request["deadline"] - time.monotonic())))
        except Exception:
            logger.warning("OI watch delivery read barrier failed")
        finally:
            request["complete"].set()
            self._queue.task_done()

    async def close(self) -> None:
        """Flush queued terminal events, then stop this process's writer."""
        if not await self.flush():
            logger.warning("OI watch outbox shutdown with unpersisted events")
        self._stop.set()
        await asyncio.to_thread(self._thread.join, 1.0)

    def _run(self) -> None:
        try:
            if self._factory is None:
                from supabase import create_client
                if not settings.supabase_url or not settings.supabase_service_role_key:
                    raise ValueError("service-role Supabase credentials required")
                client = create_client(settings.supabase_url, settings.supabase_service_role_key)
            else:
                client = self._factory()
            if self._stop.is_set():
                self._ready.set()
                return
            client.rpc("restart_oi_watches", {"p_producer_run_id": self.producer_run_id}).execute()
        except Exception as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        while not self._stop.is_set():
            try:
                rpc, params = self._queue.get(timeout=0.1)
            except Empty:
                continue
            if rpc == "__wait_delivery__":
                self._read_delivery_barrier(client, params)
                continue
            # Retry the same immutable request, preserving ordering even when a
            # register succeeded remotely but its response was lost.
            while not self._stop.is_set():
                try:
                    client.rpc(rpc, params).execute()
                    self._queue.task_done()
                    break
                except Exception:
                    logger.warning("OI watch outbox write failed; retrying %s", rpc)
                    self._stop.wait(0.5)


async def initialize_oi_watch_outbox():
    """Return the durable owner, or enable the original unavailable fallback."""
    if not settings.oi_watch_jev_enabled:
        return None
    outbox = OIWatchOutbox()
    return outbox if await outbox.start() else None
