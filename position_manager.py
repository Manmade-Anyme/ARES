import asyncio
import uuid
import threading
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from typing import List, Dict, Any, Optional, Tuple, Callable

from options_math import option_sizing_context, option_sizing_summary
from models import AresSignal, ATMStrikes
from config import settings
from alerts import send_trade_update
from atr_trailing import completed_atr_bars
from oi_watch_context import parse_candle_timestamp
from storage import AnalyticsLogger


class PositionManager:
    """
    Manages active trades, evaluates trailing stops based on live spot price,
    and persists state to Supabase.
    
    Implements lazy initialization: if the database is unreachable on startup,
    it will retry connecting during the update loop until successful.
    """
    def __init__(self):
        self.supabase: Client = create_client(
            settings.supabase_url,
            settings.supabase_key
        )
        self.analytics = AnalyticsLogger()
        self.active_trades: List[Dict[str, Any]] = []
        self._trade_writes: Dict[str, asyncio.Task] = {}
        self._execution_snapshots = {}
        self._execution_ml_bound = set()
        self._context_locks = {}
        self._pending_target_milestones = {}
        self._pending_reference_registrations = {}
        self.is_initialized = False
        self._initialize_db()

    def _queue_trade_write(self, trade_id: str, write: Callable[[], Any]):
        """Serialize each trade's writes without blocking the loop or other trades."""
        loop = asyncio.get_running_loop()
        previous = self._trade_writes.get(trade_id)

        async def _write_after_previous():
            try:
                if previous is not None:
                    await previous
                await loop.run_in_executor(None, write)
            except Exception as e:
                print(f"Failed to persist active trade {trade_id}: {e}")
            finally:
                if self._trade_writes.get(trade_id) is asyncio.current_task():
                    self._trade_writes.pop(trade_id, None)

        self._trade_writes[trade_id] = loop.create_task(_write_after_previous())

    def _initialize_db(self):
        """
        Loads all non-closed trades from Supabase into the active memory list,
        regardless of the date they were opened. Trades ride across sessions
        until their initial stop or post-T1 ATR close exit. Closed rows stay in the table untouched
        (trade_analytics remains the permanent record).
        """
        try:
            response = self.supabase.table("active_trades").select("*").execute()
            records = response.data

            # A process can stop after terminal telemetry is committed but
            # before trade_analytics and ml_collection are updated. Reconcile
            # those durable terminal rows before filtering them from memory.
            # Reconciliation is best-effort: an ML table outage must not discard
            # valid active trades or disable live SL/target monitoring.
            if any(
                record.get("state") in ["CLOSED", "STOPPED_OUT"]
                and record.get("exit_type")
                for record in records
            ):
                try:
                    from ml_signal.backfill_labels import (
                        backfill_labels,
                        repair_stuck_open_trades,
                    )
                    repair_stuck_open_trades(self.supabase, apply=True)
                    backfill_labels(self.supabase, apply=True)
                except Exception as e:
                    print(f"Failed to reconcile terminal trades during startup: {e}")

            # A crash can leave active_trades one transition behind after the
            # analytics exit write has committed. Reconcile those rows by
            # trade id before loading memory so the same trade cannot exit
            # twice after restart.
            try:
                terminal_states = {
                    "CLOSED", "T2_HIT", "SL_HIT", "STOPPED_OUT",
                    "STOPPED_OUT_AT_BE", "ATR_SL_HIT",
                }
                active_ids = [
                    record["id"] for record in records
                    if record.get("id") is not None
                    and record.get("state") not in ["CLOSED", "STOPPED_OUT"]
                ]
                if active_ids:
                    analytics_rows = self.supabase.table("trade_analytics").select(
                        "id,result_state,exit_price,exit_timestamp,pnl_points,market_context"
                    ).in_("id", active_ids).execute().data
                    analytics_by_id = {
                        str(row["id"]): row
                        for row in analytics_rows
                        if row.get("id") is not None
                    }
                    for record in records:
                        if record.get("state") in ["CLOSED", "STOPPED_OUT"]:
                            continue
                        analytics_row = analytics_by_id.get(str(record.get("id")))
                        if analytics_row is None:
                            continue
                        # Entry RPCs store context only in analytics. Restore its
                        # latest persisted paper telemetry for carried-trade alerts.
                        context = analytics_row.get("market_context")
                        if isinstance(context, dict):
                            existing = record.get("market_context")
                            record["market_context"] = {
                                **(existing if isinstance(existing, dict) else {}),
                                **context,
                            }
                        atr = record.get("market_context", {}).get("atr_trailing") or {}
                        if atr.get("state") == "T1_HIT":
                            record["state"] = "T1_HIT"
                            record["stop_loss"] = atr["stop_loss"]
                        if analytics_row.get("result_state") not in terminal_states:
                            continue
                        final_state = analytics_row["result_state"]
                        update_data = {
                            "state": "STOPPED_OUT" if final_state == "STOPPED_OUT" else "CLOSED",
                            "exit_type": final_state,
                        }
                        for field in ("exit_price", "exit_timestamp"):
                            if analytics_row.get(field) is not None:
                                update_data[field] = analytics_row[field]
                        record.update(update_data)
                        try:
                            self.supabase.table("active_trades").update(
                                update_data
                            ).eq("id", record["id"]).execute()
                        except Exception as e:
                            print(
                                f"Failed to mark desynced active trade {record['id']} terminal: {e}"
                            )
            except Exception as e:
                print(f"Failed to reconcile active trades against analytics: {e}")

            valid_trades = [
                record for record in records
                if record.get("state") not in ["CLOSED", "STOPPED_OUT"]
            ]

            self.active_trades = valid_trades
            self.is_initialized = True
            print(f"PositionManager initialized. Loaded {len(self.active_trades)} active trades (multi-day carry enabled).")
        except Exception as e:
            self.is_initialized = False
            print(f"Failed to initialize PositionManager DB: {e}")

    def find_duplicate_trade(self, signal: AresSignal, spot: float):
        """Check before order dispatch as well as before the atomic trade insert."""
        for existing in self.active_trades:
            if existing.get("state") in ["CLOSED", "STOPPED_OUT"]:
                continue
            if (existing.get("setup_type") == signal.setup_type.value
                    and existing.get("direction") == signal.direction.value
                    and abs(float(existing.get("entry_price", 0.0)) - float(spot)) <= settings.trade_dedupe_tolerance_pts):
                return existing
        return None

    async def add_trade(self, signal: AresSignal, spot: float, atm: ATMStrikes = None) -> tuple[str, str]:
        """
        Formats an AresSignal, pushes it to Supabase as OPEN using atomic RPC,
        and stores it in memory. Returns (trade_id, binding_status).
        """
        reference = signal.market_context.get("tracking_mode") == "SIGNAL_REFERENCE"
        if reference:
            for existing in self.active_trades:
                if str(existing.get("signal_uuid") or existing.get("signal_id")) == str(signal.id):
                    signal.trade_id = existing["id"]
                    return signal.trade_id, "BOUND"
            pending = self._pending_reference_registrations.get(str(signal.id))
            if pending is not None:
                trade_id, status = await self._persist_trade_entry(*pending)
                signal.trade_id, signal.trade_binding_status = trade_id, status
                return trade_id, status
        elif self.find_duplicate_trade(signal, spot):
            return "", "DUPLICATE_SKIPPED"

        identity = "reference" if reference else "paper" if signal.market_context.get("execution") else None
        trade_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ares:{identity}:{signal.id}")) if identity else str(uuid.uuid4())
        
        # Prepare RPC payload
        entry_timestamp = (signal.market_context.get("execution", {}).get("filled_at")
                           or __import__("storage").to_utc_iso(signal.timestamp))
        trade_data = {
            "id": trade_id,
            "entry_timestamp": entry_timestamp,
            "signal_id": str(signal.id),
            "signal_uuid": str(signal.id),
            "display_id": signal.display_id,
            "setup_type": signal.setup_type.value,
            "direction": signal.direction.value,
            "entry_price": float(spot),
            "stop_loss": float(signal.stop_loss),
            "target_1": float(signal.target_1),
            "target_2": float(signal.target_2),
            "state": "OPEN",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "added_time_ist": datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%H:%M:%S")
        }
        # Market context for analytics
        db_reasons = list(signal.reasons)
        if getattr(signal, "suggested_lots", None) is not None:
            db_reasons.append(option_sizing_summary(signal))
        market_context = {
            "reasons": db_reasons,
            "spot_at_signal": float(signal.trigger_price),
            "confidence": signal.confidence,
            "entry_spot": float(spot)
        }
        market_context.update(getattr(signal, "market_context", {}) or {})
        if getattr(signal, "suggested_lots", None) is not None:
            market_context["options_sizing"] = {
                **option_sizing_context(signal),
                **market_context.get("options_sizing", {}),
            }
        if getattr(signal, "oi_wall_context", None) is not None:
            market_context["oi_wall"] = signal.oi_wall_context

        trade_data["market_context"] = market_context

        oi_data = {}
        if atm is not None:
            ce_oi = atm.ce.oi if atm.ce else None
            pe_oi = atm.pe.oi if atm.pe else None
            pcr = round(pe_oi / ce_oi, 4) if (ce_oi is not None and pe_oi is not None and ce_oi > 0) else None
            ce_change = round(atm.ce.oi_change_pct, 2) if (atm.ce and atm.ce.oi_change_pct is not None) else None
            pe_change = round(atm.pe.oi_change_pct, 2) if (atm.pe and atm.pe.oi_change_pct is not None) else None
            oi_data = {
                "pcr": pcr,
                "atm_ce_oi": ce_oi,
                "atm_pe_oi": pe_oi,
                "ce_oi_change_pct": ce_change,
                "pe_oi_change_pct": pe_change,
            }

        mode = settings.signal_schema_mode
        if mode not in {"bridge", "greenfield"}:
            raise ValueError(f"Unsupported signal schema mode: {mode}")
        payload = {
            "p_trade_id": trade_id,
            "p_signal_uuid": str(signal.id),
            "p_display_id": signal.display_id,
            "p_setup_type": signal.setup_type.value,
            "p_direction": signal.direction.value,
            "p_entry_price": float(spot),
            "p_stop_loss": float(signal.stop_loss),
            "p_target_1": float(signal.target_1),
            "p_target_2": float(signal.target_2),
            "p_added_time_ist": trade_data["added_time_ist"],
            "p_market_context": market_context,
            "p_oi_data": oi_data,
            "p_entry_timestamp": entry_timestamp,
        }
        if mode == "bridge":
            payload["p_legacy_signal_id"] = signal.db_id
        payload = deepcopy(payload)
        trade_data = deepcopy(trade_data)
        def _insert():
            rpc_name = f"create_trade_entry_{mode}"
            response = self.supabase.rpc(rpc_name, deepcopy(payload)).execute()
            persisted_id = getattr(response, "data", None)
            if isinstance(persisted_id, list) and len(persisted_id) == 1:
                persisted_id = persisted_id[0]
            if str(persisted_id) != trade_id:
                raise RuntimeError(f"{rpc_name} returned an unexpected trade id")
            return True

        try:
            return await self._persist_trade_entry(signal, trade_data, _insert)
        except Exception:
            if reference:
                self._pending_reference_registrations[str(signal.id)] = (signal, trade_data, _insert)
                signal.trade_binding_status = "REGISTRATION_PENDING"
            raise

    async def _persist_trade_entry(self, signal, trade_data, insert, attempts=3):
        """Retry an unchanged entry payload and register each trade once in memory."""
        trade_id = trade_data["id"]
        loop = asyncio.get_running_loop()
        attempt = 1
        while True:
            try:
                success = await loop.run_in_executor(None, insert)
                if success:
                    signal.trade_id, signal.trade_binding_status = trade_id, "BOUND"
                    if not any(trade["id"] == trade_id for trade in self.active_trades):
                        self.active_trades.append(trade_data)
                    return trade_id, "BOUND"
            except Exception as e:
                print(f"Failed to push new trade to Supabase (attempt {attempt}): {e}")
                if attempt == attempts:
                    raise RuntimeError(f"Database insertion failed for atomic trade entry after {attempts} attempts")
                attempt += 1
                await asyncio.sleep(1)

    async def retry_reference_registrations(self):
        """Retain failed manual entries until registration and snapshot linkage recover."""
        for key, (signal, trade_data, insert) in tuple(self._pending_reference_registrations.items()):
            try:
                if not any(trade["id"] == trade_data["id"] for trade in self.active_trades):
                    await self._persist_trade_entry(signal, trade_data, insert, attempts=1)
                signal.trade_id, signal.trade_binding_status = trade_data["id"], "BOUND"
                if await self.sync_execution(signal, None):
                    self._pending_reference_registrations.pop(key, None)
            except Exception as exc:
                print(f"Reference trade registration retry failed for {signal.id}: {exc}")

    async def sync_execution(self, signal: AresSignal, execution: Optional[dict]) -> bool:
        """Persist paper fill telemetry and bind a snapshot after a delayed entry.

        The signal UUID guard prevents a delayed fill from labeling an unrelated
        ML row. Return False until that snapshot exists and is linked, so even
        a terminal paper fill remains retryable after main writes the snapshot.
        Passing None links a recovered manual entry without adding paper telemetry.
        """
        trade_id = signal.trade_id
        snapshot = dict(execution) if execution is not None else None
        mode = settings.signal_schema_mode
        if mode not in {"bridge", "greenfield"}:
            raise ValueError(f"Unsupported signal schema mode: {mode}")
        signal_column = "signal_uuid" if mode == "bridge" else "signal_id"

        def persist():
            if snapshot is not None and self._execution_snapshots.get(trade_id) != snapshot:
                with self._context_locks.setdefault(trade_id, threading.Lock()):
                    response = self.supabase.table("trade_analytics").select(
                        "market_context"
                    ).eq("id", trade_id).execute()
                    if not response.data:
                        raise RuntimeError("Paper trade analytics row is unavailable")
                    context = dict(response.data[0].get("market_context") or {})
                    context["execution"] = snapshot
                    updated = self.supabase.table("trade_analytics").update(
                        {"market_context": context}
                    ).eq("id", trade_id).execute()
                    if not updated.data:
                        raise RuntimeError("Paper execution update was not persisted")
                self._execution_snapshots[trade_id] = snapshot
            if trade_id not in self._execution_ml_bound:
                updated = self.supabase.table("ml_collection").update({
                    "trade_id": trade_id, "trade_binding_status": "BOUND",
                }).eq(signal_column, str(signal.id)).is_("trade_id", "null").execute()
                if updated.data:
                    self._execution_ml_bound.add(trade_id)
                else:
                    existing = self.supabase.table("ml_collection").select("trade_id").eq(
                        signal_column, str(signal.id)
                    ).eq("trade_id", trade_id).execute()
                    if existing.data:
                        self._execution_ml_bound.add(trade_id)

        await asyncio.get_running_loop().run_in_executor(None, persist)
        return trade_id in self._execution_ml_bound

    async def update_trades(
        self,
        spot_price: float,
        candle_high: Optional[float] = None,
        candle_low: Optional[float] = None,
        candle_timestamp: Optional[datetime] = None,
    ) -> List[Tuple[str, str]]:
        """
        Loops through active trades and evaluates live price action against the active trailing stops.
        If state changes or SL is hit, updates the row in Supabase and triggers a Discord alert.

        Before T1, intrabar detection (TASK-172, audit item 11): SL/T1 touches are
        checked against the candle's high/low when provided, not just the poll
        close. Post-T1 ATR exits are handled separately on completed 5-minute closes;
        initial protection falls back to spot_price when candle extremes are omitted.
        Two accounting rules follow from this:
          - Fill-at-level: events are reported/logged at the touched level
            (stop or target price), not at the close that detected them.
          - Optimistic resolution: if one candle touches both the stop and a
            target, the target is assumed to have filled first (wicks that
            touch targets are counted as wins).

        Returns a list of (trade_id, update_type) for every state change this
        tick, so the caller can react (e.g. clear the engine cooldown on SL_HIT).
        """
        events: List[Tuple[str, str]] = []
        if not self.is_initialized:
            self._initialize_db()
            if not self.is_initialized:
                return events  # Skip update if still not initialized

        # A successful alert is not repeated while a failed context write retries.
        for trade_id, marker in tuple(self._pending_target_milestones.items()):
            if trade_id not in self._trade_writes:
                self._queue_trade_write(
                    trade_id,
                    lambda t_id=trade_id, snapshot=deepcopy(marker):
                        self._persist_trade_context(t_id, t2_snapshot=snapshot),
                )

        high = candle_high if candle_high is not None else spot_price
        low = candle_low if candle_low is not None else spot_price
        event_ts_base = __import__("storage").to_utc_iso(candle_timestamp) if candle_timestamp else datetime.now(timezone.utc).isoformat()

        for trade in self.active_trades:
            if trade["state"] in ["CLOSED", "STOPPED_OUT"]:
                continue
            
            event_ts = event_ts_base
            if trade.get("entry_timestamp"):
                entry_ts_val = trade["entry_timestamp"]
                def to_float(ts):
                    if isinstance(ts, (int, float)):
                        return float(ts)
                    try:
                        return datetime.fromisoformat(str(ts).replace('Z', '+00:00')).timestamp()
                    except ValueError:
                        return 0.0
                if to_float(event_ts) < to_float(entry_ts_val):
                    print(f"Warning: event_ts {event_ts} precedes entry {entry_ts_val} for {trade['id']}, clamping.")
                    event_ts = entry_ts_val

            state_changed = False
            update_type = None
            event_price = spot_price
            direction = trade["direction"]

            trade_high, trade_low = high, low
            context = trade.get("market_context") or {}
            if (context.get("execution") or context.get("tracking_mode") == "SIGNAL_REFERENCE") and candle_timestamp is not None and trade.get("entry_timestamp"):
                candle_ts = __import__("storage").to_utc_iso(candle_timestamp)
                if datetime.fromisoformat(candle_ts) <= datetime.fromisoformat(trade["entry_timestamp"]):
                    # A limit filled after this candle opened; its earlier wicks
                    # cannot close the newly filled paper position retroactively.
                    trade_high = trade_low = spot_price

            # After T1 only a completed five-minute close can exit the runner.
            if trade["state"] == "T1_HIT":
                if await self._record_t2_touch(trade, trade_high, trade_low, event_ts, candle_timestamp, spot_price):
                    events.append((trade["id"], "T2_TOUCHED"))
                continue
            sign = 1 if direction == "BULLISH" else -1
            target_hit = trade_high >= trade["target_1"] if sign == 1 else trade_low <= trade["target_1"]
            stop_hit = trade_low <= trade["stop_loss"] if sign == 1 else trade_high >= trade["stop_loss"]
            if target_hit:
                trade["state"] = "T1_HIT"
                trade["stop_loss"] = trade["entry_price"]
                # A completed source minute is known only at its end. Never
                # apply an earlier five-minute close to a newly activated runner.
                activated = (parse_candle_timestamp(candle_timestamp) + timedelta(minutes=1)
                             if candle_timestamp else datetime.now(timezone.utc))
                trade.setdefault("market_context", {})["atr_trailing"] = {
                    "last_bar": activated.isoformat(),
                }
                state_changed = True
                update_type = "T1_HIT"
                event_price = trade["target_1"]
            elif stop_hit:
                trade["state"] = "CLOSED"
                state_changed = True
                update_type = "SL_HIT"
                event_price = trade["stop_loss"]

            if state_changed:
                events.append((trade["id"], update_type))
                await self._record_trade_update(trade, event_price, update_type, event_ts)
                if trade["state"] == "T1_HIT" and await self._record_t2_touch(
                    trade, trade_high, trade_low, event_ts, candle_timestamp, spot_price
                ):
                    events.append((trade["id"], "T2_TOUCHED"))

        return events

    async def _record_t2_touch(self, trade, high, low, event_ts, candle_timestamp, spot_price):
        """Record one spot milestone without changing the ATR runner lifecycle."""
        target = trade.get("target_2")
        context = trade.get("market_context") or {}
        if target is None or (context.get("target_milestones") or {}).get("t2"):
            return False
        if candle_timestamp is not None and trade.get("entry_timestamp"):
            source_at = parse_candle_timestamp(candle_timestamp)
            entry_at = parse_candle_timestamp(trade["entry_timestamp"])
            if source_at + timedelta(minutes=1) <= entry_at:
                return False
            if source_at <= entry_at:
                high = low = spot_price
                event_ts = __import__("storage").to_utc_iso(entry_at)
        touched = high >= target if trade["direction"] == "BULLISH" else low <= target
        if not touched:
            return False
        marker = {"touched_at": event_ts, "target_spot": target}
        trade.setdefault("market_context", {}).setdefault("target_milestones", {})["t2"] = marker
        self._pending_target_milestones[trade["id"]] = deepcopy(marker)
        self._queue_trade_write(
            trade["id"],
            lambda t_id=trade["id"], snapshot=deepcopy(marker):
                self._persist_trade_context(t_id, t2_snapshot=snapshot),
        )
        try:
            await send_trade_update(trade, target, "T2_TOUCHED")
        except Exception as e:
            print(f"Failed to send trade update alert: {e}")
        return True

    def _persist_trade_context(self, trade_id, atr_snapshot=None, t2_snapshot=None):
        """Merge owned context keys under the shared execution/ATR context lock."""
        if atr_snapshot is None and t2_snapshot is None:
            return
        with self._context_locks.setdefault(trade_id, threading.Lock()):
            response = self.supabase.table("trade_analytics").select("market_context").eq("id", trade_id).execute()
            if not response.data:
                raise RuntimeError("ATR trade analytics row is unavailable")
            context = dict(response.data[0].get("market_context") or {})
            if atr_snapshot is not None:
                context["atr_trailing"] = atr_snapshot
            if t2_snapshot is not None:
                context["target_milestones"] = {
                    **(context.get("target_milestones") or {}), "t2": t2_snapshot,
                }
            updated = self.supabase.table("trade_analytics").update({"market_context": context}).eq("id", trade_id).execute()
            if t2_snapshot is not None:
                if not updated.data:
                    raise RuntimeError("T2 milestone update was not persisted")
                if self._pending_target_milestones.get(trade_id) == t2_snapshot:
                    self._pending_target_milestones.pop(trade_id, None)


    async def _record_trade_update(self, trade, event_price, update_type, event_ts, pnl_points_override=None):
        """Persist each lifecycle transition through the existing ordered writer."""
        event_loop = asyncio.get_event_loop()
        # Prepare update payload
        update_data = {
            "state": trade["state"],
            "stop_loss": trade["stop_loss"]
        }
        if trade["state"] in ["CLOSED", "STOPPED_OUT"]:
            update_data["exit_price"] = float(event_price)
            update_data["exit_type"] = update_type
            update_data["exit_timestamp"] = event_ts
            if pnl_points_override is not None:
                update_data["pnl_points_override"] = float(pnl_points_override)

        # Push update to Supabase asynchronously
        atr = trade.get("market_context", {}).get("atr_trailing")
        if atr is not None:
            atr.update(state=trade["state"], stop_loss=trade["stop_loss"])
        atr_snapshot = deepcopy(atr)
        t2_snapshot = deepcopy((trade.get("market_context", {}).get("target_milestones") or {}).get("t2"))

        def _update(t_id=trade["id"], data=update_data, snapshot=atr_snapshot, milestone=t2_snapshot):
            terminal = data["state"] in ["CLOSED", "STOPPED_OUT"]
            if terminal:
                self.supabase.table("active_trades").update(data).eq("id", t_id).execute()
            self._persist_trade_context(t_id, snapshot, milestone)
            if not terminal:
                self.supabase.table("active_trades").update(data).eq("id", t_id).execute()

        def _log_exit(
            t_id=trade["id"],
            exit_price=event_price,
            final_state=update_type,
            pnl_override=pnl_points_override,
            exit_ts=event_ts,
        ):
            try:
                if pnl_override is not None:
                    self.analytics.log_exit(
                        t_id,
                        exit_price,
                        final_state,
                        exit_timestamp=exit_ts,
                        pnl_points_override=pnl_override,
                    )
                else:
                    self.analytics.log_exit(t_id, exit_price, final_state, exit_timestamp=exit_ts)
            except Exception as e:
                print(f"Failed to log trade exit to Analytics: {e}")

        def _update_and_log_exit(
            update_fn=_update,
            log_fn=_log_exit,
        ):
            try:
                update_fn()
            except Exception as e:
                print(f"Failed to persist active trade state: {e}")
            try:
                event_loop.call_soon_threadsafe(log_fn)
            except RuntimeError as e:
                print(f"Failed to schedule trade exit analytics: {e}")

        try:
            write = (
                _update_and_log_exit
                if trade["state"] in ["CLOSED", "STOPPED_OUT"]
                else _update
            )
            self._queue_trade_write(trade["id"], write)
        except Exception as e:
            print(f"Failed to update trade in Supabase: {e}")
        if update_type is not None:
            try:
                await send_trade_update(trade, event_price, update_type)
            except Exception as e:
                print(f"Failed to send trade update alert: {e}")

    async def update_atr_trailing(self, candles):
        """Ratchet post-T1 stops and exit at completed five-minute closes only."""
        candles = list(candles or ())
        bars = completed_atr_bars(candles)
        events = []
        if not bars:
            return events
        # completed_atr_bars validates each emitted bar's five source minutes.
        # Keep its final timestamp revision so invalid/forming data cannot
        # supply a milestone to a bar the ATR helper rejected.
        source_minutes = {}
        for candle in candles:
            if not isinstance(candle, dict):
                continue
            try:
                source_minutes[parse_candle_timestamp(candle.get("timestamp"))] = candle
            except (ValueError, TypeError, OverflowError, OSError):
                continue
        for trade in self.active_trades:
            if trade["state"] != "T1_HIT":
                continue
            context = trade.setdefault("market_context", {})
            atr = context.setdefault("atr_trailing", {})
            # Old carried runners lack a T1 timestamp. Resume from the latest
            # complete bar without replaying their unknown historical lifecycle.
            if not atr.get("last_bar"):
                atr["last_bar"] = bars[-1]["timestamp"]
                await self._record_trade_update(trade, bars[-1]["close"], None, bars[-1]["timestamp"])
                continue
            sign = 1 if trade["direction"] == "BULLISH" else -1
            for bar in bars:
                bar_end = parse_candle_timestamp(bar["timestamp"])
                previous_bar = parse_candle_timestamp(atr["last_bar"])
                if bar_end <= previous_bar:
                    continue
                # main processes ATR history before its latest-candle monitor.
                # Report eligible minute touches before this bar can close the
                # runner, excluding history before T1 activation / last_bar.
                for minute in range(5):
                    source_at = bar_end - timedelta(minutes=5 - minute)
                    if source_at + timedelta(minutes=1) <= previous_bar:
                        continue
                    source = source_minutes[source_at]
                    if await self._record_t2_touch(
                        trade, float(source["high"]), float(source["low"]),
                        __import__("storage").to_utc_iso(source_at), source_at, float(source["close"]),
                    ):
                        events.append((trade["id"], "T2_TOUCHED"))
                atr["last_bar"] = bar["timestamp"]
                old_stop = trade["stop_loss"]
                atr["previous_sl"] = old_stop
                update_type = None
                pnl = None
                if sign * (bar["close"] - old_stop) < 0:
                    trade["state"] = "CLOSED"
                    update_type = "ATR_SL_HIT"
                    pnl = sign * ((trade["target_1"] - trade["entry_price"]) +
                                  (bar["close"] - trade["entry_price"])) / 2
                else:
                    candidate = bar["bullish_sl" if sign == 1 else "bearish_sl"]
                    values = [old_stop, trade["entry_price"]]
                    if candidate is not None:
                        values.append(candidate)
                    trade["stop_loss"] = round(max(values) if sign == 1 else min(values), 2)
                    if trade["stop_loss"] != old_stop:
                        update_type = "ATR_SL_UPDATED"
                atr["atr"] = bar["atr"]
                await self._record_trade_update(trade, bar["close"], update_type,
                                                __import__("storage").to_utc_iso(bar["timestamp"]), pnl)
                if update_type:
                    events.append((trade["id"], update_type))
                if trade["state"] == "CLOSED":
                    break
        return events
