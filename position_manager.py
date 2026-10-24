import asyncio
import uuid
import threading
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from typing import List, Dict, Any, Optional, Tuple, Callable

from options_math import option_sizing_context, option_sizing_summary
from models import AresSignal, ATMStrikes, OptionTrailingContract, OptionTrailingContext, OptionTrailingExit
from config import settings
from alerts import send_trade_update, send_option_lifecycle_event
from atr_trailing import completed_atr_bars, calculate_option_atr_bars, ratchet_option_stop
from options_candles import (load_option_candles, contiguous_option_candles,
                             expected_completed_option_bar_end, parse_option_candle)
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
                    "CLOSED", "SL_HIT", "STOPPED_OUT",
                    "STOPPED_OUT_AT_BE", "ATR_SL_HIT", "OPTION_SL_FLOOR_HIT",
                }
                active_ids = [
                    record["id"] for record in records
                    if record.get("id") is not None
                    and record.get("state") not in ["CLOSED", "STOPPED_OUT"]
                ]
                if active_ids:
                    analytics_rows = self.supabase.table("trade_analytics").select(
                        "id,result_state,exit_price,exit_timestamp,pnl_points,market_context,score"
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

                        opt_trailing = record.get("market_context", {}).get("option_trailing") or {}
                        if opt_trailing.get("policy_version") == "option_atr_v1":
                            phase = opt_trailing.get("phase")
                            if phase == "POST_T2":
                                record["state"] = "T2_REACHED"
                            elif phase == "POST_T1":
                                record["state"] = "T1_HIT"
                                record["stop_loss"] = record["entry_price"]

                        # A trade remains active while result_state is OPEN (e.g. milestones T1, T2 reached)
                        if analytics_row.get("result_state") == "OPEN":
                            continue

                        final_state = analytics_row.get("result_state")
                        if final_state not in terminal_states and not (
                            final_state == "T2_HIT" and opt_trailing.get("policy_version") != "option_atr_v1"
                        ):
                            continue
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

            # Terminal trades can still own a committed undelivered exit alert.
            terminal_records = [r for r in records if r.get("state") in {"CLOSED", "STOPPED_OUT"}]
            self._option_alert_trades = []
            if terminal_records:
                try:
                    rows = self.supabase.table("trade_analytics").select("id,market_context").in_(
                        "id", [r["id"] for r in terminal_records]).execute().data
                    contexts = {str(r["id"]): r.get("market_context") or {} for r in rows}
                    for record in terminal_records:
                        context = contexts.get(str(record["id"]), {})
                        opt = context.get("option_trailing") or {}
                        if opt.get("policy_version") == "option_atr_v1" and any(
                            event.get("status") in {"PENDING", "FAILED"} and event.get("snapshot")
                            for event in (opt.get("alert_events") or {}).values()
                        ):
                            record["market_context"] = context
                            self._option_alert_trades.append(record)
                except Exception as exc:
                    print(f"Failed to restore terminal option alert outbox: {exc}")
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

        # Never opt a trade into the candidate policy merely because it has a strike.
        if (settings.option_atr_enabled and settings.option_atr_parity_version == "option_atr_v1"
                and settings.supabase_service_role_key):
            opt_sizing = market_context.get("options_sizing") or {}
            contract = self._validated_option_contract(opt_sizing)
            if contract and opt_sizing.get("option_sl") is not None and opt_sizing.get("entry_limit") is not None:
                market_context["option_trailing"] = OptionTrailingContext(
                    contract=contract,
                    original_option_sl=float(opt_sizing["option_sl"]),
                    original_option_entry=float(opt_sizing["entry_limit"]),
                    index_guard_stop=float(signal.stop_loss),
                ).to_dict()
                market_context["option_trailing"]["activated_at"] = (
                    signal.market_context.get("execution", {}).get("filled_at") or trade_data["created_at"])
                market_context["option_trailing"]["indicator_history_from"] = (
                    parse_candle_timestamp(signal.timestamp).date() - timedelta(days=10)).isoformat()
            else:
                market_context["option_atr_unavailable"] = "Incomplete pinned option bracket metadata"

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
                    enrolled = (trade_data.get("market_context") or {}).get("option_trailing")
                    if enrolled:
                        signal.market_context["option_trailing"] = deepcopy(enrolled)
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
        *,
        include_option_policy: bool = True,
        only_option_policy: bool = False,
        event_time: Optional[datetime] = None,
        only_trade_id: Optional[str] = None,
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
        await self.retry_option_lifecycle_writes()
        if not self.is_initialized:
            self._initialize_db()
            if not self.is_initialized:
                return events  # Skip update if still not initialized

        high = candle_high if candle_high is not None else spot_price
        low = candle_low if candle_low is not None else spot_price
        observation_time = event_time or candle_timestamp or datetime.now(timezone.utc)
        event_ts_base = __import__("storage").to_utc_iso(observation_time)

        for trade in self.active_trades:
            if trade["state"] in ["CLOSED", "STOPPED_OUT"] or (only_trade_id is not None and trade["id"] != only_trade_id):
                continue

            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            versioned = opt.get("policy_version") == "option_atr_v1"
            if (versioned and not include_option_policy) or (only_option_policy and not versioned):
                continue
            observation_high, observation_low = high, low
            if versioned:
                tracking_start = opt.get("activated_at") or trade.get("entry_timestamp")
                if tracking_start and parse_candle_timestamp(event_ts_base) < parse_candle_timestamp(tracking_start):
                    continue
                observed_at = parse_candle_timestamp(event_ts_base)
                t1_at = parse_candle_timestamp(opt["t1_at"]) if opt.get("t1_at") else None
                if candle_timestamp is not None and t1_at is not None and observed_at <= t1_at:
                    continue
                if candle_timestamp is not None and t1_at is not None and parse_candle_timestamp(candle_timestamp) < t1_at:
                    # The bar straddles activation; its earlier wicks cannot hit a new guard.
                    observation_high = observation_low = spot_price
                expected = expected_completed_option_bar_end(observed_at)
                available = getattr(self, "_option_available_until", {}).get(trade["id"])
                if (opt.get("phase") == "POST_T1" and expected is not None
                        and (available is None or available < expected)):
                    queued = opt.setdefault("pending_index_observations", [])
                    sign = 1 if trade["direction"] == "BULLISH" else -1
                    touches_t2 = observation_high >= trade["target_2"] if sign == 1 else observation_low <= trade["target_2"]
                    touches_guard = observation_low <= trade["entry_price"] if sign == 1 else observation_high >= trade["entry_price"]
                    # Preserve the first meaningful touch, rather than persisting
                    # every unchanged tick during a provider outage.
                    already_t2 = any(item.get("touches_t2") for item in queued)
                    observation = {"event_at": event_ts_base, "price": spot_price,
                                   "high": observation_high, "low": observation_low,
                                   "touches_t2": touches_t2, "touches_guard": touches_guard,
                                   "candle_timestamp": candle_timestamp.isoformat() if candle_timestamp else None}
                    needs_observation = touches_guard or (touches_t2 and not already_t2)
                    if needs_observation and (not opt.get("observed_index_guard_exit") or event_time is not None) and observation not in queued:
                        queued.append(observation)
                        if touches_guard:
                            opt["observed_index_guard_exit"] = {"event_at": event_ts_base,
                                "index_price": trade["entry_price"], "status": "AWAITING_OPTION_BACKFILL"}
                        await self._record_trade_update(trade, None, None, event_ts_base)
                    continue
                previous_index = opt.get("last_index_event_at")
                if candle_timestamp is not None and previous_index and parse_candle_timestamp(event_ts_base) <= parse_candle_timestamp(previous_index):
                    continue
                if candle_timestamp is not None:
                    opt["last_index_event_at"] = event_ts_base
                opt["last_index_mark"] = {"price": spot_price, "event_at": event_ts_base}
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

            trade_high, trade_low = observation_high, observation_low
            context = trade.get("market_context") or {}
            if (versioned or context.get("execution") or context.get("tracking_mode") == "SIGNAL_REFERENCE") and candle_timestamp is not None and trade.get("entry_timestamp"):
                candle_ts = __import__("storage").to_utc_iso(candle_timestamp)
                if parse_candle_timestamp(candle_ts) <= parse_candle_timestamp(opt.get("activated_at") or trade["entry_timestamp"]):
                    # A limit filled after this candle opened; its earlier wicks
                    # cannot close the newly filled paper position retroactively.
                    trade_high = trade_low = spot_price

            # Post-T2, runner is governed exclusively by option ATR (index SL disabled)
            if trade["state"] == "T2_REACHED":
                continue

            sign = 1 if direction == "BULLISH" else -1
            opt_ctx = (trade.get("market_context") or {}).get("option_trailing")
            is_option_atr = opt_ctx is not None and opt_ctx.get("policy_version") == "option_atr_v1"

            # Post-T1 touch evaluations
            if trade["state"] == "T1_HIT":
                if not is_option_atr:
                    continue  # Legacy spot ATR handles post-T1 exits on completed 5m bars

                # Dual protection between T1 and T2: check T2 touch and index entry BE touch
                # Target-first resolution if a single bar touches both
                target_2_hit = trade_high >= trade["target_2"] if sign == 1 else trade_low <= trade["target_2"]
                be_hit = trade_low <= trade["entry_price"] if sign == 1 else trade_high >= trade["entry_price"]

                if target_2_hit:
                    trade["state"] = "T2_REACHED"
                    opt_ctx["phase"] = "POST_T2"
                    opt_ctx["highest_milestone"] = 2
                    opt_ctx["t2_at"] = event_ts
                    opt_ctx["booked_fraction"] = 0.8
                    opt_ctx["remaining_fraction"] = 0.2
                    opt_ctx["index_guard_enabled"] = False
                    opt_ctx["observed_index_guard_exit"] = None
                    if opt_ctx.get("locked_option_sl") is None:
                        opt_ctx["locked_option_sl"] = opt_ctx.get("original_option_sl")
                        opt_ctx["protection_mode"] = "ORIGINAL_OPTION_SL_FLOOR"
                    update_type = "T2_HIT"
                    event_price = trade["target_2"]
                    events.append((trade["id"], update_type))
                    await self._record_trade_update(trade, event_price, update_type, event_ts)
                elif be_hit:
                    trade["state"] = "CLOSED"
                    opt_ctx["phase"] = "CLOSED"
                    opt_ctx["exit"] = {"reason": "STOPPED_OUT_AT_BE", "event_at": event_ts,
                                       "detected_at": datetime.now(timezone.utc).isoformat(),
                                       "index_price": trade["entry_price"], "exited_fraction": 0.5}
                    opt_ctx["remaining_fraction"] = 0.0
                    opt_ctx["index_guard_enabled"] = False
                    update_type = "STOPPED_OUT_AT_BE"
                    event_price = trade["entry_price"]
                    events.append((trade["id"], update_type))
                    await self._record_trade_update(trade, event_price, update_type, event_ts)
                continue

            # Pre-T1 touch evaluations (trade state == "OPEN")
            target_1_hit = trade_high >= trade["target_1"] if sign == 1 else trade_low <= trade["target_1"]
            target_2_hit = trade_high >= trade["target_2"] if sign == 1 else trade_low <= trade["target_2"]
            stop_hit = trade_low <= trade["stop_loss"] if sign == 1 else trade_high >= trade["stop_loss"]

            if target_1_hit:
                trade["state"] = "T1_HIT"
                trade["stop_loss"] = trade["entry_price"]
                activated = (parse_candle_timestamp(candle_timestamp) + timedelta(minutes=1)
                             if candle_timestamp else datetime.now(timezone.utc))
                trade.setdefault("market_context", {})["atr_trailing"] = {
                    "last_bar": activated.isoformat(),
                }
                if is_option_atr:
                    opt_ctx["phase"] = "POST_T1"
                    opt_ctx["highest_milestone"] = 1
                    opt_ctx["t1_at"] = event_ts
                    opt_ctx["booked_fraction"] = 0.5
                    opt_ctx["remaining_fraction"] = 0.5
                    opt_ctx["index_guard_enabled"] = True
                    opt_ctx["index_guard_stop"] = trade["entry_price"]
                    self._seed_option_stop(trade, parse_candle_timestamp(event_ts))

                update_type = "T1_HIT"
                event_price = trade["target_1"]
                events.append((trade["id"], update_type))
                await self._record_trade_update(trade, event_price, update_type, event_ts)

                # Target-first: If single index observation established both T1 and T2, apply T1 then T2 exactly once
                if target_2_hit and is_option_atr:
                    trade["state"] = "T2_REACHED"
                    opt_ctx["phase"] = "POST_T2"
                    opt_ctx["highest_milestone"] = 2
                    opt_ctx["t2_at"] = event_ts
                    opt_ctx["booked_fraction"] = 0.8
                    opt_ctx["remaining_fraction"] = 0.2
                    opt_ctx["index_guard_enabled"] = False
                    opt_ctx["observed_index_guard_exit"] = None
                    if opt_ctx.get("locked_option_sl") is None:
                        opt_ctx["locked_option_sl"] = opt_ctx.get("original_option_sl")
                        opt_ctx["protection_mode"] = "ORIGINAL_OPTION_SL_FLOOR"
                    update_type_t2 = "T2_HIT"
                    event_price_t2 = trade["target_2"]
                    events.append((trade["id"], update_type_t2))
                    await self._record_trade_update(trade, event_price_t2, update_type_t2, event_ts)
                continue

            elif stop_hit:
                trade["state"] = "CLOSED"
                if is_option_atr:
                    opt_ctx["phase"] = "CLOSED"
                    opt_ctx["highest_milestone"] = 0
                    opt_ctx["booked_fraction"] = 0.0
                    opt_ctx["remaining_fraction"] = 0.0
                    opt_ctx["index_guard_enabled"] = False
                    opt_ctx["exit"] = {"reason": "SL_HIT", "event_at": event_ts,
                                       "detected_at": datetime.now(timezone.utc).isoformat(),
                                       "index_price": trade["stop_loss"], "exited_fraction": 1.0}

                update_type = "SL_HIT"
                event_price = trade["stop_loss"]
                events.append((trade["id"], update_type))
                await self._record_trade_update(trade, event_price, update_type, event_ts)
                continue

        return events


    async def _record_trade_update(self, trade, event_price, update_type, event_ts, pnl_points_override=None):
        """Persist each lifecycle transition through the existing ordered writer."""
        opt = (trade.get("market_context") or {}).get("option_trailing")
        if opt and opt.get("policy_version") == "option_atr_v1":
            await self._record_option_transition(trade, event_price, update_type, event_ts)
            return
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

        opt_trailing = trade.get("market_context", {}).get("option_trailing")
        opt_snapshot = deepcopy(opt_trailing) if opt_trailing else None

        def _update(t_id=trade["id"], data=update_data, snapshot=atr_snapshot, opt_snap=opt_snapshot):
            terminal = data["state"] in ["CLOSED", "STOPPED_OUT"]
            if terminal:
                self.supabase.table("active_trades").update(data).eq("id", t_id).execute()
            if snapshot is not None:
                with self._context_locks.setdefault(t_id, threading.Lock()):
                    response = self.supabase.table("trade_analytics").select("market_context").eq("id", t_id).execute()
                    if not response.data:
                        raise RuntimeError("ATR trade analytics row is unavailable")
                    context = dict(response.data[0].get("market_context") or {})
                    context["atr_trailing"] = snapshot
                    self.supabase.table("trade_analytics").update({"market_context": context}).eq("id", t_id).execute()
            if not terminal:
                self.supabase.table("active_trades").update(data).eq("id", t_id).execute()

        def _log_exit(
            t_id=trade["id"],
            exit_price=event_price,
            final_state=update_type,
            pnl_override=pnl_points_override,
            exit_ts=event_ts,
            score_ovr=(opt_snapshot.get("highest_milestone") if opt_snapshot else None),
        ):
            try:
                kwargs = {"exit_timestamp": exit_ts}
                if pnl_override is not None:
                    kwargs["pnl_points_override"] = pnl_override
                if score_ovr is not None:
                    kwargs["score_override"] = score_ovr
                self.analytics.log_exit(
                    t_id,
                    exit_price,
                    final_state,
                    **kwargs,
                )
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
        bars = completed_atr_bars(candles)
        events = []
        if not bars:
            return events
        for trade in self.active_trades:
            # Skip trades governed by option premium ATR trailing
            if trade.get("market_context", {}).get("option_trailing", {}).get("policy_version") == "option_atr_v1":
                continue
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
                if parse_candle_timestamp(bar["timestamp"]) <= parse_candle_timestamp(atr["last_bar"]):
                    continue
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

    @staticmethod
    def _validated_option_contract(sizing):
        """Accept instrument identity only from the already calculated bracket."""
        from math import isfinite
        try:
            security_id = str(sizing.get("security_id") or "").strip()
            expiry = str(sizing.get("expiry_date") or "")
            datetime.strptime(expiry, "%Y-%m-%d")
            strike = float(sizing.get("strike"))
            option_type = str(sizing.get("option_type") or "").upper()
            if not security_id.isdigit() or not isfinite(strike) or strike <= 0 or option_type not in {"CE", "PE"}:
                return None
            for name in ("option_sl", "entry_limit"):
                value = float(sizing[name])
                if not isfinite(value) or value <= 0:
                    return None
            return OptionTrailingContract(security_id, "NSE_FNO", expiry, int(strike), option_type)
        except (KeyError, TypeError, ValueError):
            return None

    def _option_runtime(self):
        """Lazy initialization also supports restored managers and injected test adapters."""
        for name, factory in (("_option_pending", dict), ("_option_locks", dict),
                              ("_option_revisions", dict), ("_option_history", dict),
                              ("_option_warning_sent", set), ("_option_confirmed_sessions", set)):
            if not hasattr(self, name):
                setattr(self, name, factory())

    @staticmethod
    def _weighted_option_index_pnl(trade, exit_price):
        """Model index points; the premium close is never an inferred index mark."""
        if exit_price is None:
            return None
        opt = trade["market_context"]["option_trailing"]
        milestone = opt.get("highest_milestone", 0)
        entry = trade["entry_price"]
        sign = 1 if trade["direction"] == "BULLISH" else -1
        booked = .5 * (trade["target_1"] - entry) if milestone >= 1 else 0
        if milestone >= 2:
            booked += .3 * (trade["target_2"] - entry)
        remainder = 1 if milestone == 0 else .5 if milestone == 1 else .2
        return sign * (booked + remainder * (exit_price - entry))

    async def _record_option_transition(self, trade, index_price, update_type, event_ts):
        """Keep observed protection while serializing immutable, retryable commits."""
        self._option_runtime()
        opt = trade["market_context"]["option_trailing"]
        terminal = trade["state"] in {"CLOSED", "STOPPED_OUT"}
        if terminal:
            opt["monitoring_status"] = "CLOSED"
        snapshot = deepcopy(opt)
        # Market identity is stable across retries; it does not depend on a stale revision.
        identity = ":".join(str(v) for v in (trade["id"], update_type or "WATERMARK", event_ts,
                            snapshot.get("locked_option_sl"), snapshot.get("last_processed_option_bar_end")))
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
        alert_snapshot = None
        if update_type:
            alert_snapshot = {
                "event_id": event_id, "update_type": update_type, "event_time": event_ts,
                "index_price": index_price, "trade": deepcopy(trade), "option_trailing": snapshot,
                "previous_stop": snapshot.get("previous_locked_option_sl"),
                "candle": deepcopy(snapshot.get("source_candle") or {}),
                "detail": snapshot.get("monitoring_detail"),
            }
            # Do not recursively copy the outbox into every new alert snapshot.
            alert_snapshot["option_trailing"].pop("alert_events", None)
            alert_snapshot["trade"]["market_context"]["option_trailing"].pop("alert_events", None)
        payload = {
            "active_state": trade["state"], "stop_loss": trade["stop_loss"],
            "score": snapshot.get("highest_milestone") if snapshot.get("highest_milestone") or terminal else None,
            "exit_reason": update_type if terminal else None,
            "exit_price": index_price if terminal else None,
            "exit_timestamp": event_ts if terminal else None,
            "pnl_points": self._weighted_option_index_pnl(trade, index_price) if terminal else None,
            "update_type": update_type, "option_trailing": deepcopy(opt), "alert_snapshot": alert_snapshot,
        }
        pending = self._option_pending.setdefault(trade["id"], [])
        if not any(item[0] == event_id for item in pending):
            pending.append((event_id, payload))
        await self._flush_option_trade(trade)

    async def _flush_option_trade(self, trade):
        self._option_runtime()
        trade_id = trade["id"]
        async with self._option_locks.setdefault(trade_id, asyncio.Lock()):
            opt = trade["market_context"]["option_trailing"]
            pending = self._option_pending.setdefault(trade_id, [])
            loop = asyncio.get_running_loop()
            while pending:
                event_id, payload = pending[0]
                revision = self._option_revisions.get(trade_id, opt.get("revision", 1))
                try:
                    def commit():
                        with self._context_locks.setdefault(trade_id, threading.Lock()):
                            return self.analytics.apply_option_lifecycle_transition(
                                trade_id, revision, event_id, deepcopy(payload))
                    result = await loop.run_in_executor(None, commit)
                    if not isinstance(result, dict) or result.get("success") is not True or not isinstance(result.get("revision"), int):
                        raise RuntimeError("Lifecycle transaction did not confirm a commit")
                except Exception as exc:
                    # A conflict is reloaded before retry; all other failures remain queued.
                    if "revision" in str(exc).lower() and hasattr(self.analytics, "get_option_lifecycle_state"):
                        try:
                            persisted = await loop.run_in_executor(None, lambda: self.analytics.get_option_lifecycle_state(trade_id))
                            persisted_opt = persisted.get("option_trailing") or {}
                            committed_rev = persisted_opt.get("revision")
                            if committed_rev and committed_rev != revision:
                                self._option_revisions[trade_id] = committed_rev
                                opt["revision"] = committed_rev
                                opt["alert_events"] = deepcopy(persisted_opt.get("alert_events") or {})
                                if persisted_opt.get("phase") == "CLOSED":
                                    opt.update(persisted_opt)
                                    trade["state"] = "CLOSED"
                                    pending.clear()
                                else:
                                    # Drop observations superseded by a newer committed milestone/stop.
                                    kept = []
                                    for queued_id, queued in pending:
                                        candidate = queued["option_trailing"]
                                        older_phase = candidate.get("highest_milestone", 0) < persisted_opt.get("highest_milestone", 0)
                                        older_stop = (persisted_opt.get("locked_option_sl") is not None and
                                            (candidate.get("locked_option_sl") is None or candidate["locked_option_sl"] < persisted_opt["locked_option_sl"]))
                                        terminal_observation = queued.get("active_state") in {"CLOSED", "STOPPED_OUT"}
                                        if terminal_observation:
                                            # Preserve an observed protective exit across revision repair.
                                            # A concurrent T2/index-BE conflict requires reconciliation;
                                            # it must never reopen the observed closed reference.
                                            if queued.get("exit_reason") == "STOPPED_OUT_AT_BE" and persisted_opt.get("highest_milestone", 0) >= 2:
                                                raise RuntimeError("Observed index exit conflicts with committed T2; reconciliation required")
                                            for field in ("highest_milestone", "booked_fraction"):
                                                candidate[field] = max(candidate.get(field) or 0, persisted_opt.get(field) or 0)
                                            candidate["exit"]["exited_fraction"] = round(1.0 - candidate["booked_fraction"], 10)
                                            committed_stop = persisted_opt.get("locked_option_sl")
                                            observed_stop = candidate.get("locked_option_sl")
                                            if committed_stop is not None and (observed_stop is None or committed_stop > observed_stop):
                                                candidate["locked_option_sl"] = committed_stop
                                                candidate["protection_mode"] = persisted_opt.get("protection_mode", candidate.get("protection_mode"))
                                            candidate.update(phase="CLOSED", remaining_fraction=0.0)
                                            queued["score"] = candidate["highest_milestone"]
                                            rebased_trade = deepcopy(trade)
                                            rebased_trade["market_context"]["option_trailing"] = candidate
                                            queued["pnl_points"] = self._weighted_option_index_pnl(rebased_trade, queued.get("exit_price"))
                                            alert = queued.get("alert_snapshot")
                                            if alert:
                                                alert["option_trailing"] = deepcopy(candidate)
                                                alert["option_trailing"].pop("alert_events", None)
                                                alert["trade"]["market_context"]["option_trailing"] = deepcopy(alert["option_trailing"])
                                            opt.update({key: candidate[key] for key in ("highest_milestone", "booked_fraction", "locked_option_sl", "protection_mode", "exit") if key in candidate})
                                            kept.append((queued_id, queued))
                                        elif older_phase and candidate.get("locked_option_sl") is not None and (
                                                persisted_opt.get("locked_option_sl") is None or candidate["locked_option_sl"] > persisted_opt["locked_option_sl"]):
                                            # A later database milestone must not erase a newer observed raise.
                                            for field in ("phase", "highest_milestone", "booked_fraction", "remaining_fraction",
                                                          "index_guard_enabled", "index_guard_stop", "t1_at", "t2_at",
                                                          "observed_index_guard_exit", "pending_index_observations"):
                                                if field in persisted_opt:
                                                    candidate[field] = persisted_opt[field]
                                            queued["active_state"] = persisted.get("active_state")
                                            queued["score"] = persisted_opt.get("highest_milestone")
                                            if queued.get("update_type") == "T1_HIT":
                                                queued["update_type"] = "OPTION_SL_RATCHETED"
                                                candidate["previous_locked_option_sl"] = persisted_opt.get("locked_option_sl")
                                            alert = queued.get("alert_snapshot")
                                            if alert:
                                                alert["option_trailing"] = deepcopy(candidate)
                                                alert["option_trailing"].pop("alert_events", None)
                                                alert["update_type"] = queued["update_type"]
                                                alert["previous_stop"] = persisted_opt.get("locked_option_sl")
                                                alert["trade"]["state"] = queued["active_state"]
                                                alert["trade"]["market_context"]["option_trailing"] = deepcopy(alert["option_trailing"])
                                            if trade.get("state") not in {"CLOSED", "STOPPED_OUT"}:
                                                opt.update({key: candidate[key] for key in ("phase", "highest_milestone", "booked_fraction", "remaining_fraction", "index_guard_enabled", "index_guard_stop", "locked_option_sl", "protection_mode", "observed_index_guard_exit", "pending_index_observations") if key in candidate})
                                                trade["state"] = queued["active_state"]
                                            kept.append((queued_id, queued))
                                        elif older_stop and (candidate.get("highest_milestone", 0) > persisted_opt.get("highest_milestone", 0)
                                                or (not older_phase and (candidate.get("pending_index_observations") or candidate.get("observed_index_guard_exit")))):
                                            # Milestone progress and stop strength are independent:
                                            # retain new touches/backfill guards with committed protection.
                                            candidate["locked_option_sl"] = persisted_opt["locked_option_sl"]
                                            candidate["protection_mode"] = persisted_opt.get("protection_mode", candidate.get("protection_mode"))
                                            alert = queued.get("alert_snapshot")
                                            if alert:
                                                alert["option_trailing"] = deepcopy(candidate)
                                                alert["option_trailing"].pop("alert_events", None)
                                                alert["trade"]["market_context"]["option_trailing"] = deepcopy(alert["option_trailing"])
                                            # Later queued bars may already have raised live protection.
                                            if opt.get("locked_option_sl") is None or candidate["locked_option_sl"] > opt["locked_option_sl"]:
                                                opt.update({key: candidate[key] for key in ("locked_option_sl", "protection_mode")})
                                            kept.append((queued_id, queued))
                                        elif not older_phase and not older_stop:
                                            kept.append((queued_id, queued))
                                    for _, kept_payload in kept:
                                        candidate = kept_payload["option_trailing"]
                                        if candidate.get("phase") == persisted_opt.get("phase") == "POST_T1":
                                            # Preserve both writers' deferred sources in every queued
                                            # snapshot, including later raises and silent watermarks.
                                            # CLOSED/T2 transitions have consumed these observations.
                                            contexts = [candidate]
                                            if opt.get("phase") == "POST_T1":
                                                contexts.append(opt)
                                            for context in contexts:
                                                observations = deepcopy(persisted_opt.get("pending_index_observations") or [])
                                                if context is opt:
                                                    observations = deepcopy(candidate.get("pending_index_observations") or observations)
                                                for observation in context.get("pending_index_observations") or []:
                                                    if observation not in observations:
                                                        observations.append(deepcopy(observation))
                                                observations.sort(key=lambda item: parse_candle_timestamp(item["event_at"]))
                                                context["pending_index_observations"] = observations
                                                guards = [guard for guard in (context.get("observed_index_guard_exit"),
                                                          candidate.get("observed_index_guard_exit"),
                                                          persisted_opt.get("observed_index_guard_exit")) if guard]
                                                guards.extend({"event_at": observation["event_at"], "index_price": trade["entry_price"],
                                                               "status": "AWAITING_OPTION_BACKFILL"}
                                                              for observation in observations if observation.get("touches_guard"))
                                                if guards:
                                                    context["observed_index_guard_exit"] = deepcopy(min(guards,
                                                        key=lambda guard: parse_candle_timestamp(guard["event_at"])))
                                            alert = kept_payload.get("alert_snapshot")
                                            if alert:
                                                alert["option_trailing"] = deepcopy(candidate)
                                                alert["option_trailing"].pop("alert_events", None)
                                                alert["trade"]["market_context"]["option_trailing"] = deepcopy(alert["option_trailing"])
                                        for field in ("last_processed_option_bar_end", "last_valid_option_bar_end", "last_index_event_at"):
                                            committed_at = persisted_opt.get(field)
                                            if committed_at and (not candidate.get(field) or parse_candle_timestamp(candidate[field]) < parse_candle_timestamp(committed_at)):
                                                candidate[field] = committed_at
                                    pending[:] = kept
                                    if not pending:
                                        opt.update(persisted_opt)
                                        trade["state"] = persisted.get("active_state", trade["state"])
                                        trade["stop_loss"] = persisted.get("stop_loss", trade["stop_loss"])
                                continue
                        except Exception:
                            pass
                    if trade_id not in self._option_warning_sent:
                        self._option_warning_sent.add(trade_id)
                        try:
                            await send_option_lifecycle_event({
                                "update_type": "PERSISTENCE_FAILED", "event_time": (payload.get("alert_snapshot") or {}).get("event_time") or payload.get("exit_timestamp") or datetime.now(timezone.utc).isoformat(),
                                "trade": deepcopy(trade), "option_trailing": deepcopy(opt),
                                "detail": f"Observed {payload.get('update_type') or 'monitoring update'} at {payload.get('exit_timestamp') or (payload.get('alert_snapshot') or {}).get('event_time')}; database pending",
                            })
                        except Exception:
                            pass
                    return
                self._option_revisions[trade_id] = result["revision"]
                opt["revision"] = result["revision"]
                # Only commit bookkeeping is reconciled: newer observed protection must survive.
                committed_opt = result.get("option_trailing") or {}
                if "alert_events" in committed_opt:
                    opt["alert_events"] = deepcopy(committed_opt["alert_events"])
                pending.pop(0)
                self._option_warning_sent.discard(trade_id)
            await self._deliver_option_outbox(trade)

    def _ack_option_alert(self, trade_id, event_id, status):
        # Execution telemetry still replaces context under this same process lock.
        # Claims and final acknowledgements must not race that read/merge/write.
        with self._context_locks.setdefault(trade_id, threading.Lock()):
            return self.analytics.acknowledge_option_lifecycle_alert(trade_id, event_id, status)

    async def _deliver_option_outbox(self, trade):
        """Persist intent before transport; uncertain submissions are never blindly retried."""
        opt = trade["market_context"]["option_trailing"]
        alerts = opt.get("alert_events") or {}
        loop = asyncio.get_running_loop()
        for event_id, event in alerts.items():
            if event.get("status") not in {"PENDING", "FAILED"}:
                continue
            snapshot = event.get("snapshot")
            if not snapshot:
                continue
            event_type = snapshot.get("update_type")
            phase = opt.get("phase")
            stale = ((opt.get("observed_index_guard_exit") and phase != "CLOSED" and event_type not in {"MONITORING_UNAVAILABLE", "MONITORING_RESTORED"}) or (phase == "CLOSED" and event_type not in {"SL_HIT", "STOPPED_OUT_AT_BE", "ATR_SL_HIT", "OPTION_SL_FLOOR_HIT"})
                     or (phase == "POST_T2" and event_type in {"T1_HIT", "ATR_ACTIVE", "MONITORING_RESTORED"}
                         and (snapshot.get("option_trailing") or {}).get("phase") != "POST_T2"))
            try:
                status = "SUPPRESSED" if stale else "SENDING"
                claim = await loop.run_in_executor(None, lambda: self._ack_option_alert(trade["id"], event_id, status))
                if not stale and claim.get("claimed") is not True:
                    event["status"] = "AMBIGUOUS"
                    continue
                event["status"] = status
                if stale:
                    continue
                try:
                    result = await send_option_lifecycle_event(deepcopy(snapshot))
                except Exception:
                    result = "AMBIGUOUS"
                event["status"] = result if result in {"DELIVERED", "FAILED", "AMBIGUOUS"} else "AMBIGUOUS"
                await loop.run_in_executor(None, lambda: self._ack_option_alert(trade["id"], event_id, event["status"]))
            except Exception:
                # SENDING remains durable if the final acknowledgement is lost.
                continue

    async def retry_option_lifecycle_writes(self):
        """Retry committed outbox and failed observed decisions on every monitor poll."""
        self._option_runtime()
        for trade in list(getattr(self, "active_trades", [])) + list(getattr(self, "_option_alert_trades", [])):
            if (trade.get("market_context") or {}).get("option_trailing", {}).get("policy_version") == "option_atr_v1":
                await self._flush_option_trade(trade)

    def _seed_option_stop(self, trade, event_at):
        """Arm the last line known before T1 without replaying pre-activation closes."""
        self._option_runtime()
        opt = trade["market_context"]["option_trailing"]
        history = self._option_history.get(trade["id"], [])
        confirmed = [bar for bar in history if parse_candle_timestamp(bar.end_time) < event_at]
        if not confirmed:
            return
        bar = confirmed[-1]
        expected = expected_completed_option_bar_end(event_at - timedelta(microseconds=1))
        if expected is not None and parse_candle_timestamp(bar.end_time) < expected:
            return
        stop, mode, raised = ratchet_option_stop(opt.get("locked_option_sl"), bar.raw_line,
                                                opt["original_option_sl"], protection_mode=opt.get("protection_mode", "NONE"))
        opt["raw_line"] = bar.raw_line
        opt["last_valid_option_bar_end"] = bar.end_time
        if raised:
            opt.update(locked_option_sl=stop, protection_mode=mode, atr_eligible=mode == "ATR",
                       stop_effective_at=event_at.isoformat())

    def _prepare_option_history(self, histories, observed_at):
        self._option_runtime()
        result = {}
        for trade in self.active_trades:
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            if opt.get("policy_version") != "option_atr_v1":
                continue
            sec_id = str((opt.get("contract") or {}).get("security_id") or "")
            raw = histories.get(sec_id, []) if isinstance(histories, dict) else histories or []
            if not isinstance(histories, dict):
                # Unkeyed inputs need explicit identity; never apply an anonymous
                # premium series to every open option contract.
                raw = [bar for bar in raw if str(bar.get("security_id") if isinstance(bar, dict)
                       else getattr(bar, "security_id", None)) == sec_id]
            normalized = load_option_candles(raw, expected_security_id=sec_id, observed_at=observed_at)
            self._option_confirmed_sessions.update(bar.start_time.date() for bar in normalized)
            if trade.get("entry_timestamp"):
                from options_candles import IST
                self._option_confirmed_sessions.add(parse_candle_timestamp(trade["entry_timestamp"]).astimezone(IST).date())
            history_from = opt.get("indicator_history_from")
            if history_from:
                normalized = [bar for bar in normalized if bar.start_time.date().isoformat() >= history_from]
            contiguous = contiguous_option_candles(normalized)
            bars = calculate_option_atr_bars(contiguous)
            # Validate against this trade's durable progress before publishing
            # availability or caching history used to release index observations.
            if bars and opt.get("last_processed_option_bar_end"):
                from options_candles import OptionCandle
                cutoff = parse_candle_timestamp(opt["last_processed_option_bar_end"])
                first = next((bar for bar in bars if parse_candle_timestamp(bar.end_time) > cutoff), None)
                if first and not any(parse_candle_timestamp(bar.end_time) == cutoff for bar in bars):
                    probe = [OptionCandle(cutoff-timedelta(minutes=5), cutoff, 1,1,1,1,0),
                             OptionCandle(parse_candle_timestamp(first.start_time), parse_candle_timestamp(first.end_time),1,1,1,1,0)]
                    if len(contiguous_option_candles(probe)) != 2:
                        bars = []
            result[trade["id"]] = bars
            # Keep previously confirmed history through a transient outage.
            if bars:
                self._option_history[trade["id"]] = bars
        return result

    async def update_option_atr_trailing(self, candles, current_time=None, *, _check_monitoring=True):
        """Process native completed premium bars; old stop is checked before a raise."""
        now = current_time or datetime.now(timezone.utc)
        histories = self._prepare_option_history(candles, now)
        if not getattr(self, "_option_replaying", False):
            self._option_available_until = {key: parse_candle_timestamp(bars[-1].end_time) for key, bars in histories.items() if bars}
        events = []
        await self.retry_option_lifecycle_writes()
        for trade in self.active_trades:
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            if opt.get("policy_version") != "option_atr_v1" or trade["state"] in {"CLOSED", "STOPPED_OUT"}:
                continue
            bars = histories.get(trade["id"], [])
            phase = opt.get("phase")
            if phase not in {"POST_T1", "POST_T2"}:
                continue
            cutoff = parse_candle_timestamp(opt.get("last_processed_option_bar_end") or opt["t1_at"])
            if opt.get("locked_option_sl") is None:
                self._seed_option_stop(trade, parse_candle_timestamp(opt["t1_at"]))
            for bar in bars:
                end = parse_candle_timestamp(bar.end_time)
                if end < cutoff or (end == cutoff and opt.get("last_processed_option_bar_end")):
                    continue
                opt["source_candle"] = {"start_time": bar.start_time, "end_time": bar.end_time, "close": bar.close}
                opt["last_valid_option_bar_end"] = bar.end_time
                active_stop = opt.get("locked_option_sl")
                if active_stop is None and phase == "POST_T2":
                    active_stop = opt["original_option_sl"]
                opt["last_processed_option_bar_end"] = bar.end_time
                if active_stop is not None and bar.close < active_stop:
                    reason = "ATR_SL_HIT" if opt.get("protection_mode") == "ATR" else "OPTION_SL_FLOOR_HIT"
                    remainder = opt["remaining_fraction"]
                    mark = opt.get("last_index_mark") or {}
                    # An index mark is contemporaneous only at the same source close.
                    index_price = mark.get("price") if mark.get("event_at") and parse_candle_timestamp(mark["event_at"]) == end else None
                    opt["exit"] = {"reason": reason, "event_at": bar.end_time,
                        "detected_at": datetime.now(timezone.utc).isoformat(), "option_close": bar.close,
                        "option_stop": active_stop, "index_price": index_price,
                        "index_price_at": mark.get("event_at") if index_price is not None else None,
                        "exited_fraction": remainder, "candle_start": bar.start_time, "candle_end": bar.end_time}
                    opt.update(phase="CLOSED", remaining_fraction=0.0, index_guard_enabled=False)
                    trade["state"] = "CLOSED"
                    await self._record_trade_update(trade, index_price, reason, bar.end_time)
                    events.append((trade["id"], reason))
                    break
                opt["raw_line"] = bar.raw_line
                old = opt.get("locked_option_sl")
                stop, mode, raised = ratchet_option_stop(old, bar.raw_line, opt["original_option_sl"], protection_mode=opt.get("protection_mode", "NONE"))
                update = None
                if raised:
                    update = "ATR_ACTIVE" if opt.get("protection_mode") != "ATR" and mode == "ATR" else "OPTION_SL_RATCHETED"
                    opt.update(previous_locked_option_sl=old, locked_option_sl=stop, protection_mode=mode,
                               atr_eligible=mode == "ATR", stop_effective_at=bar.end_time)
                await self._record_trade_update(trade, None, update, bar.end_time)
                if update:
                    events.append((trade["id"], update))
            if trade["state"] == "CLOSED":
                continue
            if not _check_monitoring:
                continue
            expected = expected_completed_option_bar_end(now)
            valid = parse_candle_timestamp(bars[-1].end_time) if bars else None
            unavailable = expected is not None and expected.date() in self._option_confirmed_sessions and now > expected + timedelta(seconds=60) and (valid is None or valid < expected)
            status = opt.get("monitoring_status", "ACTIVE")
            if unavailable and status != "UNAVAILABLE":
                opt["monitoring_status"] = "UNAVAILABLE"
                opt["monitoring_detail"] = "Missing, invalid, stale, or discontinuous pinned option history; stop retained"
                await self._record_trade_update(trade, None, "MONITORING_UNAVAILABLE", now.isoformat())
            elif not unavailable and valid is not None and expected is not None and valid >= expected and status == "UNAVAILABLE":
                opt["monitoring_status"] = "RESTORED"
                await self._record_trade_update(trade, None, "MONITORING_RESTORED", now.isoformat())
        return events

    async def update_option_market_events(self, index_candles, option_histories, current_time=None):
        """Merge missed source events, index milestones first at equal event times."""
        now = current_time or datetime.now(timezone.utc)
        histories = self._prepare_option_history(option_histories, now)
        self._option_available_until = {key: parse_candle_timestamp(bars[-1].end_time) for key, bars in histories.items() if bars}
        timeline = {}
        option_trades = [trade for trade in self.active_trades
                         if trade.get("state") not in {"CLOSED", "STOPPED_OUT"}
                         and (trade.get("market_context") or {}).get("option_trailing", {}).get("policy_version") == "option_atr_v1"]
        for candle in index_candles or []:
            raw = candle if isinstance(candle, dict) else {
                "timestamp": candle.timestamp, "open": candle.open, "high": candle.high,
                "low": candle.low, "close": candle.close, "complete": True,
            }
            if raw.get("complete") is not True:
                continue
            normalized = parse_option_candle(raw, observed_at=now, timeframe_minutes=1)
            if normalized is None:
                continue
            self._option_confirmed_sessions.add(normalized.start_time.date())
            if not any(normalized.end_time > parse_candle_timestamp(
                    trade["market_context"]["option_trailing"].get("last_index_event_at") or trade["market_context"]["option_trailing"].get("activated_at") or trade["entry_timestamp"])
                    for trade in option_trades):
                continue
            timeline.setdefault(normalized.end_time, []).append({
                "timestamp": normalized.start_time, "open": normalized.open,
                "high": normalized.high, "low": normalized.low, "close": normalized.close,
            })
        for trade in option_trades:
            opt = trade["market_context"]["option_trailing"]
            cutoff = parse_candle_timestamp(opt.get("last_processed_option_bar_end") or opt.get("t1_at") or opt.get("activated_at") or trade["entry_timestamp"])
            for bar in histories.get(trade["id"], []):
                end = parse_candle_timestamp(bar.end_time)
                if end > cutoff or (end == cutoff and not opt.get("last_processed_option_bar_end")):
                    timeline.setdefault(end, [])
        pending_ticks = {}
        for trade in self.active_trades:
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            if opt.get("policy_version") != "option_atr_v1":
                continue
            queued = list(opt.get("pending_index_observations") or [])
            for observation in queued:
                event_at = parse_candle_timestamp(observation["event_at"])
                timeline.setdefault(event_at, [])
                pending_ticks.setdefault(event_at, []).append((trade["id"], observation))
            # If unavailable, update_trades requeues the observation below.
            opt["pending_index_observations"] = []
        events = []
        self._option_replaying = True
        try:
            return await self._replay_option_timeline(timeline, pending_ticks, option_histories, now, events)
        finally:
            self._option_replaying = False

    async def _replay_option_timeline(self, timeline, pending_ticks, option_histories, now, events):
        for event_at in sorted(timeline):
            for candle in timeline[event_at]:
                val = (lambda key: candle[key]) if isinstance(candle, dict) else (lambda key: getattr(candle, key))
                events.extend(await self.update_trades(val("close"), candle_high=val("high"), candle_low=val("low"),
                    candle_timestamp=parse_candle_timestamp(val("timestamp")), event_time=event_at, only_option_policy=True))
            for trade_id, tick in pending_ticks.get(event_at, []):
                events.extend(await self.update_trades(tick["price"], candle_high=tick["high"], candle_low=tick["low"],
                    candle_timestamp=parse_candle_timestamp(tick["candle_timestamp"]) if tick.get("candle_timestamp") else None,
                    event_time=event_at, only_option_policy=True, only_trade_id=trade_id))
            events.extend(await self.update_option_atr_trailing(option_histories, current_time=event_at, _check_monitoring=False))
        # Staleness is measured against detection time only after chronological recovery.
        events.extend(await self.update_option_atr_trailing(option_histories, current_time=now))
        return events
