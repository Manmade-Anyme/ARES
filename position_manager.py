import asyncio
import uuid
import threading
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from typing import List, Dict, Any, Optional, Tuple, Callable

from options_math import option_sizing_context, option_sizing_summary
from execution.reference_prices import option_target_evidence, spot_exit_evidence, is_premium_trigger_exit
from math import isfinite
from models import AresSignal, ATMStrikes, OptionTrailingContract, OptionTrailingContext, OptionTrailingExit
from config import settings
from alerts import send_trade_update, send_option_lifecycle_event
from atr_trailing import completed_atr_bars, completed_spot_minutes, calculate_option_atr_bars, ratchet_option_stop
from options_candles import (load_option_candles, contiguous_option_candles,
                             expected_completed_option_bar_end, parse_option_candle)
from oi_watch_context import parse_candle_timestamp
from storage import AnalyticsLogger

_OPTION_POLICY_VERSIONS = {"option_atr_v1", "option_premium_protection_v2"}


def _has_option_policy(opt):
    return isinstance(opt, dict) and opt.get("policy_version") in _OPTION_POLICY_VERSIONS


def _is_option_instrument(trade):
    context = trade.get("market_context") or {}
    opt = context.get("option_trailing") or {}
    sizing = context.get("options_sizing") or {}
    reference_contract = (context.get("option_reference") or {}).get("contract") or {}
    option_type = str(sizing.get("option_type") or reference_contract.get("option_type") or "").upper()
    return (_has_option_policy(opt) or option_type in {"CE", "PE"}
            or str(sizing.get("security_id") or "").isdigit())


def _valid_pinned_option_quote_contract(contract, observed_at):
    """Validate the structural fields required before a premium quote can protect a trade."""
    if not isinstance(contract, dict):
        return False
    security_id = str(contract.get("security_id") or "")
    expiry = contract.get("expiry")
    option_type = str(contract.get("option_type") or "").upper()
    strike = contract.get("strike")
    try:
        expiry_date = datetime.strptime(str(expiry), "%Y-%m-%d").date()
        strike_value = int(strike)
    except (TypeError, ValueError, OverflowError):
        return False
    if isinstance(strike, bool) or str(strike_value) != str(strike):
        return False
    return (security_id.isdigit() and contract.get("exchange_segment") == "NSE_FNO"
            and strike_value > 0 and option_type in {"CE", "PE"}
            and observed_at.date() <= expiry_date)


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
        try:
            from quote_service import get_quote_service
            get_quote_service()
        except Exception:
            pass
        self.analytics = AnalyticsLogger()
        self.active_trades: List[Dict[str, Any]] = []
        self._option_execution_route_trades: List[Dict[str, Any]] = []
        self._option_alert_trades: List[Dict[str, Any]] = []
        self._trade_writes: Dict[str, asyncio.Task] = {}
        self._trade_alerts: Dict[str, asyncio.Task] = {}
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

    def _queue_trade_alert(self, trade, price, update_type):
        """Keep a slow T2 webhook off the protective path and preserve order."""
        trade_id = trade["id"]
        alerts = self.__dict__.setdefault("_trade_alerts", {})
        previous = alerts.get(trade_id)
        snapshot = deepcopy(trade)

        async def send_after_previous():
            try:
                if previous is not None:
                    await previous
                await send_trade_update(snapshot, price, update_type)
            except Exception as e:
                print(f"Failed to send trade update alert: {e}")
            finally:
                if alerts.get(trade_id) is asyncio.current_task():
                    alerts.pop(trade_id, None)

        alerts[trade_id] = asyncio.create_task(send_after_previous())

    async def flush_pending_trade_updates(self, timeout=12.0):
        """Give queued persistence and ordered alerts a bounded graceful drain."""
        self._option_runtime()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        for trade in list(getattr(self, "active_trades", [])) + list(getattr(self, "_option_alert_trades", [])):
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            alerts = opt.get("alert_events") or {}
            if (_has_option_policy(opt)
                    and (self._option_pending.get(trade["id"])
                         or any(event.get("status") in {"PENDING", "FAILED"}
                                for event in alerts.values()))):
                self._schedule_option_flush(trade)
        tasks = (set(self._trade_writes.values())
                 | set(getattr(self, "_trade_alerts", {}).values())
                 | set(getattr(self, "_option_flush_tasks", {}).values()))
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - loop.time()))
            if pending:
                print(f"Trade update shutdown drain timed out with {len(pending)} task(s) pending")
                return False
        # An earlier failed write may have finished before shutdown started.
        # Retry its frozen milestone once within the same shutdown deadline.
        milestones = getattr(self, "_pending_target_milestones", {})
        reference_contexts = getattr(self, "_pending_option_reference_context", {})
        pending_context_ids = set(milestones) | set(reference_contexts)
        if pending_context_ids and deadline > loop.time():
            for trade_id in tuple(pending_context_ids):
                marker = milestones.get(trade_id)
                refs = reference_contexts.get(str(trade_id))
                self._queue_trade_write(
                    trade_id,
                    lambda t_id=trade_id, snapshot=deepcopy(marker), reference_snapshot=deepcopy(refs):
                        self._persist_trade_context(
                            t_id, t2_snapshot=snapshot,
                            reference_milestone_snapshot=reference_snapshot,
                        ),
                )
            tasks = set(self._trade_writes.values())
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - loop.time()))
                if pending:
                    print(f"Trade update shutdown drain timed out with {len(pending)} task(s) pending")
                    return False
        if milestones or reference_contexts:
            print("Trade update shutdown drain left target milestone context unpersisted")
            return False
        pending_option = {trade_id: len(events) for trade_id, events in
                          getattr(self, "_option_pending", {}).items() if events}
        if pending_option:
            print(f"Trade update shutdown drain left option lifecycle writes pending: {pending_option}")
            return False
        return True

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
                        if _has_option_policy(opt_trailing):
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
                            final_state == "T2_HIT" and not _has_option_policy(opt_trailing)
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

            # Terminal trades can still own committed alerts or paper-exit
            # routes. Restore their latest analytics context before filtering
            # them from active lifecycle monitoring.
            terminal_records = [r for r in records if r.get("state") in {"CLOSED", "STOPPED_OUT"}]
            self._option_alert_trades = []
            self._option_execution_route_trades = []
            if terminal_records:
                try:
                    rows = self.supabase.table("trade_analytics").select("id,market_context").in_(
                        "id", [r["id"] for r in terminal_records]).execute().data
                    contexts = {str(r["id"]): r.get("market_context") or {} for r in rows}
                    for record in terminal_records:
                        context = contexts.get(str(record["id"]), {})
                        if not isinstance(context, dict):
                            context = {}
                        existing = record.get("market_context")
                        record["market_context"] = {
                            **(existing if isinstance(existing, dict) else {}),
                            **context,
                        }
                        opt = context.get("option_trailing") or {}
                        if _has_option_policy(opt) and any(
                            event.get("status") in {"PENDING", "FAILED"} and event.get("snapshot")
                            for event in (opt.get("alert_events") or {}).values()
                        ):
                            self._option_alert_trades.append(record)
                        execution = context.get("execution") or {}
                        if not isinstance(execution, dict):
                            execution = {}
                        if (opt.get("policy_version") == "option_premium_protection_v2"
                                and (opt.get("execution_route_pending") is True
                                     or (execution.get("mode") == "PAPER"
                                         and execution.get("status") == "EXIT_PENDING"))):
                            self._option_execution_route_trades.append(record)
                except Exception as exc:
                    print(f"Failed to restore terminal option outboxes: {exc}")
            valid_trades = [
                record for record in records
                if record.get("state") not in ["CLOSED", "STOPPED_OUT"]
            ]

            self.active_trades = valid_trades
            self.is_initialized = True
            self._cleanup_option_selection_if_terminal()
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

        # Baseline premium protection is independent of the ATR feature gate.
        # It is enabled only when the dedicated atomic v2 backend contract exists.
        opt_sizing = market_context.get("options_sizing") or {}
        contract = self._validated_option_contract(opt_sizing)
        protection_ready = False
        if (contract is not None and bool(settings.supabase_service_role_key)
                and callable(getattr(self.analytics, "option_premium_protection_ready", None))):
            try:
                protection_ready = await asyncio.to_thread(self.analytics.option_premium_protection_ready)
                protection_ready = protection_ready is True
            except Exception:
                logger.warning("Option premium protection capability check failed", exc_info=True)
        atr_enabled = (settings.option_atr_enabled
                       and settings.option_atr_approved_version == "option_atr_v1"
                       and bool(settings.supabase_service_role_key))
        if contract is not None and protection_ready:
            execution = market_context.get("execution") or {}
            try:
                fill_qty = int(execution.get("filled_quantity") or 0)
                fill_price = float(execution.get("fill_price") or 0)
            except (TypeError, ValueError, OverflowError):
                fill_qty, fill_price = 0, 0.0
            confirmed_fill = (fill_qty > 0 and execution.get("protection_confirmed") is True
                              and isfinite(fill_price) and fill_price > 0)
            entry_basis = fill_price if confirmed_fill else float(opt_sizing["entry_limit"])
            entry_source = "CONFIRMED_EXECUTION_AVERAGE" if confirmed_fill else "REFERENCE_ENTRY_LIMIT"
            initial_sl = float(opt_sizing["option_sl"])
            v2_context = OptionTrailingContext(
                policy_version="option_premium_protection_v2",
                contract=contract,
                original_option_sl=initial_sl,
                original_option_entry=entry_basis,
                entry_premium_basis_source=entry_source,
                index_guard_stop=float(signal.stop_loss),
                locked_option_sl=initial_sl,
                protection_mode="INITIAL_OPTION_SL",
                premium_stop_source="INITIAL_OPTION_SL",
                atr_enabled=atr_enabled,
                atr_eligible=False,
                execution_status="CONFIRMED_PAPER_FILL" if confirmed_fill else "REFERENCE_ONLY",
                entry_premium_basis_at=(execution.get("filled_at") if confirmed_fill else entry_timestamp),
            )
            market_context["option_trailing"] = v2_context.to_dict()
            market_context["option_trailing"]["activated_at"] = (
                execution.get("filled_at") or trade_data["created_at"])
            market_context["option_trailing"]["accounting_formula_version"] = "one_lot_reference_v1"
            market_context["option_trailing"]["indicator_history_from"] = (
                parse_candle_timestamp(signal.timestamp).date() - timedelta(days=10)).isoformat()
        elif contract is not None:
            market_context["option_protection_unavailable"] = (
                "option_premium_protection_v2 backend capability is not ready; no spot ATR fallback")
        elif opt_sizing:
            market_context["option_protection_unavailable"] = "Incomplete pinned option bracket metadata"

        # Report-reference accounting has a distinct schema gate and state
        # object. It does not require or create option ATR/protection state.
        market_context.pop("option_reference", None)
        reference_requested = (
            reference and settings.option_reference_accounting_enabled
            and bool(settings.supabase_service_role_key)
        )
        if reference_requested:
            try:
                reference_ready = await asyncio.to_thread(self.analytics.option_reference_capture_ready)
                reference_ready = reference_ready is True
            except Exception:
                logger.warning("Option reference capture capability check failed", exc_info=True)
                reference_ready = False
            opt_sizing = market_context.get("options_sizing") or {}
            contract = self._validated_option_reference_contract(opt_sizing, entry_timestamp)
            if reference_ready and contract:
                market_context["option_reference"] = {
                    "formula_version": "one_lot_hold_to_exit_v1",
                    "revision": 1,
                    "status": "TRACKING",
                    "reference_lots": 1,
                    "contract": contract,
                    "entry_event_at": entry_timestamp,
                    "entry": None,
                    "milestones": {},
                    "exit_event_at": None,
                    "exit_reason": None,
                    "exit": None,
                    "premium_points": None,
                    "applied_events": {},
                }
            elif reference_ready:
                logger.info("Option reference accounting skipped for trade %s: pinned contract is incomplete", trade_id)

        if not _has_option_policy(market_context.get("option_trailing") or {}):
            # New spot-ATR registrations participate in the T2 notification
            # milestone. Previously stored trades remain gated until reviewed.
            market_context["legacy_t2_tracking_approved"] = True

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
                    reference_state = (trade_data.get("market_context") or {}).get("option_reference")
                    if reference_state:
                        signal.market_context["option_reference"] = deepcopy(reference_state)
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

    @staticmethod
    def _mark_option_execution_route_pending(trade, reason):
        opt = (trade.get("market_context") or {}).get("option_trailing") or {}
        if (opt.get("policy_version") != "option_premium_protection_v2"
                or opt.get("execution_status") in {"REFERENCE_ONLY", "REFERENCE_TRIGGERED_NO_BROKER_FILL"}):
            return
        if opt.get("execution_route_pending") or opt.get("execution_route_trigger"):
            return
        opt["execution_route_pending"] = True
        opt["execution_route_reason"] = reason

    @staticmethod
    def _retain_index_guard_execution_route(trade, event_at, received_at, *,
                                            index_watermark_checked=False, prior_index_watermark=None,
                                            allow_t1_boundary=False):
        """Protect a confirmed paper runner while reference history is pending."""
        from execution.signal_dispatch import SignalDispatcher

        context = trade.get("market_context") or {}
        opt = context.get("option_trailing") or {}
        execution = context.get("execution") or {}
        ambiguity_keys = ("ambiguous_index_touch", "ambiguous_premium_bar", "ambiguous_premium_quote")
        if any(opt.get(key) is not None and not isinstance(opt[key], dict) for key in ambiguity_keys):
            return False
        if (opt.get("policy_version") != "option_premium_protection_v2"
                or opt.get("phase") != "POST_T1" or opt.get("highest_milestone") != 1
                or opt.get("index_guard_enabled") is not True
                or opt.get("index_guard_stop") != trade.get("entry_price")
                or opt.get("execution_status") != "CONFIRMED_PAPER_FILL"
                or opt.get("execution_route_pending") or opt.get("execution_route_trigger")
                or opt.get("exit_trigger") or opt.get("deferred_option_exit")
                or any((opt.get(key) or {}).get("protective_option_exit") for key in ambiguity_keys)
                or not isinstance(execution, dict) or execution.get("mode") != "PAPER"
                or execution.get("protection_confirmed") is not True
                or execution.get("status") not in {"FILLED", "PARTIALLY_FILLED", "EXIT_PENDING"}
                or (execution.get("status") == "EXIT_PENDING"
                    and execution.get("exit_reason") != "T1_HIT")
                or not (execution.get("t1_hit") is True
                        or (execution.get("status") == "EXIT_PENDING"
                            and execution.get("exit_reason") == "T1_HIT"))):
            return False
        try:
            observed_at = parse_candle_timestamp(event_at)
            watermark = prior_index_watermark if index_watermark_checked else opt.get("last_index_event_at")
            t1_at = parse_candle_timestamp(opt["t1_at"]) if opt.get("t1_at") else None
            if (t1_at is not None
                    and (observed_at < t1_at or (observed_at == t1_at and not allow_t1_boundary))):
                return False
            if watermark and observed_at <= parse_candle_timestamp(watermark):
                return False
        except (TypeError, ValueError, OverflowError, OSError):
            return False
        correlation_id = str(execution.get("correlation_id") or "")
        if not correlation_id:
            return False
        saved = execution.get("order_record")
        record = (SignalDispatcher._record_from_snapshot(correlation_id, saved)
                  if isinstance(saved, dict) else SignalDispatcher._record_from_legacy_context(
                      correlation_id, trade, opt, execution))
        if (record is None
                or not (record.t1_hit is True or
                        (record.status == "EXIT_PENDING" and record.exit_reason == "T1_HIT"))
                or record.exited_quantity >= record.filled_quantity
                or record.bracket.signal_id != str(trade.get("signal_uuid") or trade.get("signal_id") or "")
                or not SignalDispatcher._matches_contract(record.bracket, opt.get("contract") or {})):
            return False
        PositionManager._mark_option_execution_route_pending(trade, "STOPPED_OUT_AT_BE")
        opt["execution_route_trigger"] = {
            "reason": "STOPPED_OUT_AT_BE", "stop_domain": "SPOT_INDEX",
            "event_at": event_at, "received_at": received_at,
            "index_price": trade["entry_price"], "source": "index_guard_observation",
        }
        return True

    async def acknowledge_option_execution_route(self, trade_id, reason):
        """Clear a durable retry marker without reopening the lifecycle writer."""
        route_trades = getattr(self, "_option_execution_route_trades", [])
        trade = next((item for item in [*self.active_trades, *route_trades]
                      if str(item.get("id")) == str(trade_id)), None)
        if trade is None:
            return False
        opt = (trade.get("market_context") or {}).get("option_trailing") or {}
        if (not opt.get("execution_route_pending")
                or opt.get("execution_route_reason") != reason):
            return False
        acknowledge = getattr(self.analytics, "acknowledge_option_execution_route", None)
        if not callable(acknowledge):
            return False
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: acknowledge(str(trade_id), reason))
        if not isinstance(result, dict) or result.get("success") is not True or result.get("acknowledged") is not True:
            return False
        opt["execution_route_pending"] = False
        execution = (trade.get("market_context") or {}).get("execution") or {}
        if trade in route_trades and execution.get("status") != "EXIT_PENDING":
            route_trades.remove(trade)
        return True

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
        market_event_time: Optional[datetime] = None,
        only_trade_id: Optional[str] = None,
        allow_t2_milestone: bool = True,
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
        For buffered ticks, market_event_time is the provider timestamp used to
        reject observations that occurred before an individual trade's entry;
        event_time remains the local arrival time used to preserve tick order.
        """
        events: List[Tuple[str, str]] = []
        await self.retry_option_lifecycle_writes(defer_for_protection=True)
        if not self.is_initialized:
            self._initialize_db()
            if not self.is_initialized:
                return events  # Skip update if still not initialized

        # A successful alert is not repeated while a failed context write retries.
        pending_t2 = getattr(self, "_pending_target_milestones", {})
        pending_reference = getattr(self, "_pending_option_reference_context", {})
        for trade_id in set(pending_t2) | set(pending_reference):
            if trade_id not in self._trade_writes:
                marker = pending_t2.get(trade_id)
                refs = pending_reference.get(str(trade_id))
                self._queue_trade_write(
                    trade_id,
                    lambda t_id=trade_id, snapshot=deepcopy(marker), reference_snapshot=deepcopy(refs):
                        self._persist_trade_context(
                            t_id, t2_snapshot=snapshot,
                            reference_milestone_snapshot=reference_snapshot,
                        ),
                )

        high = candle_high if candle_high is not None else spot_price
        low = candle_low if candle_low is not None else spot_price
        observation_time = event_time or candle_timestamp or datetime.now(timezone.utc)
        base_event_ts = __import__("storage").to_utc_iso(observation_time)
        if market_event_time is not None:
            try:
                source_at = parse_candle_timestamp(market_event_time)
                receipt_at = (parse_candle_timestamp(event_time) if event_time is not None
                              else datetime.now(timezone.utc))
                if source_at > receipt_at + timedelta(seconds=5):
                    market_event_time = None
            except (TypeError, ValueError, OverflowError, OSError):
                market_event_time = None

        for trade in self.active_trades:
            if trade["state"] in ["CLOSED", "STOPPED_OUT"] or (only_trade_id is not None and trade["id"] != only_trade_id):
                continue

            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            versioned = _has_option_policy(opt)
            event_ts_base = base_event_ts
            source_event_ts = None
            if market_event_time is not None:
                try:
                    source_event_ts = __import__("storage").to_utc_iso(market_event_time)
                except (TypeError, ValueError, OverflowError, OSError):
                    pass
            if versioned and event_time is None and candle_timestamp is not None:
                # Candle timestamps mark the minute start. Evaluate completed
                # bars at their close so a T1 tick within that minute is not
                # mistaken for an observation that predates T1.
                event_ts_base = __import__("storage").to_utc_iso(
                    candle_timestamp + timedelta(minutes=1))
            market_context = trade.get("market_context") or {}
            context = market_context
            execution = market_context.get("execution") or {}
            if versioned:
                entry_cutoff = (opt.get("activated_at") or execution.get("filled_at")
                                or trade.get("entry_timestamp"))
            elif market_context.get("tracking_mode") == "SIGNAL_REFERENCE":
                entry_cutoff = (execution.get("filled_at") or trade.get("created_at")
                                or trade.get("entry_timestamp"))
            else:
                entry_cutoff = trade.get("entry_timestamp")
            entry_at = None
            if entry_cutoff:
                try:
                    entry_at = parse_candle_timestamp(entry_cutoff)
                except (TypeError, ValueError, OverflowError, OSError):
                    pass
            if entry_at is not None and event_time is not None:
                try:
                    if parse_candle_timestamp(event_time) < entry_at:
                        continue
                except (TypeError, ValueError, OverflowError, OSError):
                    pass
            if entry_at is not None and market_event_time is not None:
                try:
                    # Dhan LTT is second-resolution. Reject only when the
                    # whole possible event second ends before this trade began.
                    if parse_candle_timestamp(market_event_time) + timedelta(seconds=1) <= entry_at:
                        continue
                except (TypeError, ValueError, OverflowError, OSError):
                    pass
            if (versioned and not include_option_policy) or (only_option_policy and not versioned):
                continue
            observation_high, observation_low = high, low
            if ((versioned or context.get("execution") or context.get("tracking_mode") == "SIGNAL_REFERENCE")
                    and candle_timestamp is not None and trade.get("entry_timestamp")):
                candle_ts = __import__("storage").to_utc_iso(candle_timestamp)
                activation_ts = opt.get("activated_at") or trade["entry_timestamp"]
                if parse_candle_timestamp(candle_ts) <= parse_candle_timestamp(activation_ts):
                    # A limit filled after this candle opened; its earlier wicks
                    # cannot close the newly filled position retroactively.
                    observation_high = observation_low = spot_price
            if versioned:
                phase_boundary_ambiguous = False
                tracking_start = opt.get("activated_at") or trade.get("entry_timestamp")
                if tracking_start and parse_candle_timestamp(event_ts_base) < parse_candle_timestamp(tracking_start):
                    continue
                if opt.get("phase") == "POST_T1" and opt.get("t1_at") and candle_timestamp is None:
                    # T1 may activate before a buffered tick batch is drained.
                    # Compare timestamps from the same clock; receipt time may be
                    # later than a stale tick's exchange time.
                    try:
                        phase_cutoff = parse_candle_timestamp(opt["t1_at"])
                    except (TypeError, ValueError, OverflowError, OSError):
                        continue
                    activation_source = opt.get("t1_activation_source")
                    phase_uses_exchange_time = (
                        market_event_time is not None
                        and (
                            activation_source in {"tick_exchange", "completed_candle"}
                            or (activation_source is None and phase_cutoff.second == 0
                                and phase_cutoff.microsecond == 0)
                        )
                    )
                    phase_observation = (
                        market_event_time if phase_uses_exchange_time
                        else event_time or event_ts_base
                    )
                    try:
                        phase_observed_at = parse_candle_timestamp(phase_observation)
                    except (TypeError, ValueError, OverflowError, OSError):
                        # A malformed provider timestamp cannot prove phase order.
                        if market_event_time is not None:
                            continue
                        phase_observed_at = None
                    if phase_observed_at is not None:
                        if phase_uses_exchange_time and phase_observed_at.microsecond == 0:
                            # A whole-second provider timestamp denotes an interval.
                            # Reject only intervals wholly before T1; a boundary
                            # overlap cannot establish whether the guard came later.
                            source_second_end = phase_observed_at + timedelta(seconds=1)
                            if source_second_end <= phase_cutoff:
                                continue
                            phase_boundary_ambiguous = phase_observed_at <= phase_cutoff < source_second_end
                        else:
                            if phase_observed_at < phase_cutoff:
                                continue
                            phase_boundary_ambiguous = phase_observed_at == phase_cutoff
                sign = 1 if trade["direction"] == "BULLISH" else -1
                guard_touched_at_boundary = (
                    observation_low <= trade["entry_price"] if sign == 1
                    else observation_high >= trade["entry_price"]
                )
                if phase_boundary_ambiguous and guard_touched_at_boundary:
                    if (opt.get("accounting_formula_version") == "one_lot_reference_v1"
                            and not opt.get("ambiguous_index_touch")):
                        opt["ambiguous_index_touch"] = {
                            "kind": "T1_PHASE_ENTRY",
                            "source_minute": phase_observed_at.replace(second=0, microsecond=0).isoformat(),
                            "event_at": phase_observed_at.isoformat(),
                            "received_at": event_ts_base,
                            "t1_at": opt["t1_at"],
                            "t1_activation_source": opt.get("t1_activation_source"),
                            "high": observation_high,
                            "low": observation_low,
                            "status": "ORDER_UNKNOWN",
                        }
                        await self._record_trade_update(trade, None, None, source_event_ts or event_ts_base)
                        continue
                    if opt.get("accounting_formula_version") != "one_lot_reference_v1":
                        continue
                observed_at = parse_candle_timestamp(source_event_ts or event_ts_base)
                t1_at = parse_candle_timestamp(opt["t1_at"]) if opt.get("t1_at") else None
                if candle_timestamp is not None and t1_at is not None and observed_at <= t1_at:
                    continue
                if (candle_timestamp is not None and t1_at is not None
                        and parse_candle_timestamp(candle_timestamp) < t1_at
                        and opt.get("accounting_formula_version") != "one_lot_reference_v1"):
                    # Existing option-ATR trades retain their historical rule:
                    # never replay pre-T1 wicks into the new entry guard.
                    observation_high = observation_low = spot_price
                expected = expected_completed_option_bar_end(observed_at)
                available = getattr(self, "_option_available_until", {}).get(trade["id"])
                requires_option_history = (
                    opt.get("policy_version") != "option_premium_protection_v2"
                    or opt.get("atr_enabled") is True
                )
                if (opt.get("phase") == "POST_T1" and requires_option_history and expected is not None
                        and (available is None or available < expected)):
                    sign = 1 if trade["direction"] == "BULLISH" else -1
                    touches_t2 = observation_high >= trade["target_2"] if sign == 1 else observation_low <= trade["target_2"]
                    touches_guard = observation_low <= trade["entry_price"] if sign == 1 else observation_high >= trade["entry_price"]
                    execution_guard_touch = touches_guard
                    if (candle_timestamp is not None and t1_at is not None
                            and parse_candle_timestamp(candle_timestamp) <= t1_at):
                        # A wick in the T1 candle may predate T1. Its close is
                        # the only value known to occur after activation.
                        execution_guard_touch = (spot_price <= trade["entry_price"] if sign == 1
                                                 else spot_price >= trade["entry_price"])
                    guard_intent = execution_guard_touch and self._retain_index_guard_execution_route(
                        trade, source_event_ts or event_ts_base, event_ts_base)
                    if guard_intent:
                        events.append((trade["id"], "STOPPED_OUT_AT_BE"))
                    t1_premium_ambiguity = (self._unresolved_t1_premium_ambiguity(opt)
                        if opt.get("policy_version") == "option_premium_protection_v2" else None)
                    if t1_premium_ambiguity and (touches_guard or touches_t2):
                        observation = {"event_at": source_event_ts or event_ts_base, "price": spot_price,
                            "high": observation_high, "low": observation_low,
                            "candle_timestamp": candle_timestamp.isoformat() if candle_timestamp else None}
                        later = t1_premium_ambiguity.setdefault("subsequent_index_observations", [])
                        if observation not in later:
                            later.append(observation)
                            guard_intent = True
                        if guard_intent:
                            await self._record_trade_update(trade, None, None, source_event_ts or event_ts_base)
                        continue
                    queued = opt.setdefault("pending_index_observations", [])
                    # Preserve the first meaningful touch, rather than persisting
                    # every unchanged tick during a provider outage.
                    already_t2 = any(item.get("touches_t2") for item in queued)
                    observation = {"event_at": source_event_ts or event_ts_base, "price": spot_price,
                                   "high": observation_high, "low": observation_low,
                                   "touches_t2": touches_t2, "touches_guard": touches_guard,
                                   "candle_timestamp": candle_timestamp.isoformat() if candle_timestamp else None}
                    needs_observation = touches_guard or (touches_t2 and not already_t2)
                    if needs_observation and (not opt.get("observed_index_guard_exit") or event_time is not None) and observation not in queued:
                        queued.append(observation)
                        if touches_guard:
                            opt["observed_index_guard_exit"] = {"event_at": source_event_ts or event_ts_base,
                                "index_price": trade["entry_price"], "status": "AWAITING_OPTION_BACKFILL"}
                        await self._record_trade_update(trade, None, None, source_event_ts or event_ts_base)
                    elif guard_intent:
                        await self._record_trade_update(trade, None, None, source_event_ts or event_ts_base)
                    continue
                previous_index = opt.get("last_index_event_at")
                if candle_timestamp is not None and previous_index and parse_candle_timestamp(event_ts_base) <= parse_candle_timestamp(previous_index):
                    continue
                if candle_timestamp is not None:
                    opt["last_index_event_at"] = event_ts_base
                opt["last_index_mark"] = {"price": spot_price, "event_at": source_event_ts or event_ts_base}
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
            price_event_ts = source_event_ts or event_ts

            state_changed = False
            update_type = None
            event_price = spot_price
            direction = trade["direction"]

            trade_high, trade_low = observation_high, observation_low

            # Post-T2, runner is governed exclusively by option ATR (index SL disabled)
            if trade["state"] == "T2_REACHED":
                continue

            sign = 1 if direction == "BULLISH" else -1
            opt_ctx = (trade.get("market_context") or {}).get("option_trailing")
            is_option_atr = _has_option_policy(opt_ctx)
            reference_accounting = is_option_atr and opt_ctx.get("accounting_formula_version") == "one_lot_reference_v1"
            transition_event_ts = price_event_ts if is_option_atr else event_ts

            # Post-T1 touch evaluations
            if trade["state"] == "T1_HIT":
                if not is_option_atr:
                    if not allow_t2_milestone:
                        continue
                    last_bar = (context.get("atr_trailing") or {}).get("last_bar")
                    if (candle_timestamp is not None and last_bar
                            and parse_candle_timestamp(candle_timestamp) + timedelta(minutes=1)
                            <= parse_candle_timestamp(last_bar)):
                        continue
                    if (event_time is not None and last_bar
                            and parse_candle_timestamp(event_time) < parse_candle_timestamp(last_bar)):
                        continue
                    if await self._record_t2_touch(trade, trade_high, trade_low, event_ts_base, candle_timestamp, spot_price):
                        events.append((trade["id"], "T2_TOUCHED"))
                    continue  # Legacy spot ATR handles exits on completed 5m bars

                # A candle's wicks do not establish which protection occurred first.
                target_2_hit = trade_high >= trade["target_2"] if sign == 1 else trade_low <= trade["target_2"]
                be_hit = trade_low <= trade["entry_price"] if sign == 1 else trade_high >= trade["entry_price"]
                execution_guard_touch = be_hit
                if (opt_ctx.get("policy_version") == "option_premium_protection_v2"
                        and candle_timestamp is not None and opt_ctx.get("t1_at")
                        and parse_candle_timestamp(candle_timestamp) <= parse_candle_timestamp(opt_ctx["t1_at"])):
                    execution_guard_touch = (spot_price <= trade["entry_price"] if sign == 1
                                             else spot_price >= trade["entry_price"])

                if ((reference_accounting or opt_ctx.get("policy_version") == "option_premium_protection_v2")
                        and opt_ctx.get("ambiguous_index_touch")):
                    ambiguity = opt_ctx["ambiguous_index_touch"]
                    if ambiguity.get("kind") == "SPOT_OPTION_EXIT_TIE":
                        try:
                            ambiguous_at = parse_candle_timestamp(ambiguity["event_at"])
                            source_at = parse_candle_timestamp(price_event_ts)
                            if source_at.microsecond == 0 and source_at <= ambiguous_at < source_at + timedelta(seconds=1):
                                # A second-resolution spot tick cannot be ordered against
                                # an option close stamped at the same boundary.
                                continue
                        except (KeyError, TypeError, ValueError, OverflowError, OSError):
                            pass
                    if target_2_hit or be_hit:
                        guard_intent = execution_guard_touch and self._retain_index_guard_execution_route(
                            trade, price_event_ts, event_ts_base,
                            index_watermark_checked=candle_timestamp is not None,
                            prior_index_watermark=previous_index if candle_timestamp is not None else None)
                        if guard_intent:
                            events.append((trade["id"], "STOPPED_OUT_AT_BE"))
                        observation = {"event_at": price_event_ts, "price": spot_price,
                            "high": trade_high, "low": trade_low,
                            "candle_timestamp": candle_timestamp.isoformat() if candle_timestamp else None}
                        later = ambiguity.setdefault("subsequent_index_observations", [])
                        changed = observation not in later
                        if changed:
                            later.append(observation)
                        if changed or guard_intent:
                            await self._record_trade_update(trade, None, None, transition_event_ts)
                    continue
                t1_premium_ambiguity = self._unresolved_t1_premium_ambiguity(opt_ctx)
                if (opt_ctx.get("policy_version") == "option_premium_protection_v2"
                        and t1_premium_ambiguity):
                    if target_2_hit or be_hit:
                        guard_intent = execution_guard_touch and self._retain_index_guard_execution_route(
                            trade, price_event_ts, event_ts_base,
                            index_watermark_checked=candle_timestamp is not None,
                            prior_index_watermark=previous_index if candle_timestamp is not None else None)
                        if guard_intent:
                            events.append((trade["id"], "STOPPED_OUT_AT_BE"))
                        observation = {"event_at": price_event_ts, "price": spot_price,
                            "high": trade_high, "low": trade_low,
                            "candle_timestamp": candle_timestamp.isoformat() if candle_timestamp else None}
                        later = t1_premium_ambiguity.setdefault("subsequent_index_observations", [])
                        changed = observation not in later
                        if changed:
                            later.append(observation)
                        if changed or guard_intent:
                            await self._record_trade_update(trade, None, None, transition_event_ts)
                    # The T1-tied premium bar/quote may have stopped the trade
                    # depending on event order. Keep later touches for reconciliation.
                    continue
                if reference_accounting and target_2_hit and be_hit and candle_timestamp is not None:
                    opt_ctx["ambiguous_index_touch"] = {
                        "kind": "T2_ENTRY", "source_minute": candle_timestamp.isoformat(), "event_at": price_event_ts,
                        "high": trade_high, "low": trade_low, "status": "ORDER_UNKNOWN",
                    }
                    guard_intent = (
                        opt_ctx.get("policy_version") == "option_premium_protection_v2"
                        and execution_guard_touch
                        and self._retain_index_guard_execution_route(
                            trade, price_event_ts, event_ts_base,
                            index_watermark_checked=True, prior_index_watermark=previous_index)
                    )
                    if guard_intent:
                        events.append((trade["id"], "STOPPED_OUT_AT_BE"))
                    await self._record_trade_update(trade, None, None, transition_event_ts)
                    continue

                if target_2_hit:
                    trade["state"] = "T2_REACHED"
                    opt_ctx["phase"] = "POST_T2"
                    opt_ctx["highest_milestone"] = 2
                    opt_ctx["t2_at"] = price_event_ts
                    opt_ctx["booked_fraction"] = 0.8
                    opt_ctx["remaining_fraction"] = 0.2
                    opt_ctx["index_guard_enabled"] = False
                    opt_ctx["observed_index_guard_exit"] = None
                    if opt_ctx.get("locked_option_sl") is None:
                        opt_ctx["locked_option_sl"] = opt_ctx.get("original_option_sl")
                        opt_ctx["protection_mode"] = "ORIGINAL_OPTION_SL_FLOOR"
                    update_type = "T2_HIT"
                    event_price = trade["target_2"]
                    self._queue_option_reference_target_touch(
                        trade, "T2", price_event_ts, trade["target_2"], spot_price, candle_timestamp,
                    )
                    if reference_accounting:
                        self._capture_reference_leg(trade, "T2", event_price, price_event_ts, candle_timestamp)
                    events.append((trade["id"], update_type))
                    await self._record_trade_update(trade, event_price, update_type, transition_event_ts)
                elif be_hit:
                    source_start = parse_candle_timestamp(candle_timestamp) if candle_timestamp is not None else None
                    source_end = source_start + timedelta(minutes=1) if source_start is not None else None
                    t1_in_source_minute = (reference_accounting and t1_at is not None
                        and source_start is not None and source_start <= t1_at <= source_end)
                    if t1_in_source_minute:
                        opt_ctx["ambiguous_index_touch"] = {
                            "kind": "T1_ENTRY", "source_minute": source_start.isoformat(),
                            "event_at": price_event_ts, "high": trade_high, "low": trade_low,
                            "status": "ORDER_UNKNOWN",
                        }
                        guard_intent = (
                            opt_ctx.get("policy_version") == "option_premium_protection_v2"
                            and execution_guard_touch
                            and self._retain_index_guard_execution_route(
                                trade, price_event_ts, event_ts_base,
                                index_watermark_checked=candle_timestamp is not None,
                                prior_index_watermark=previous_index if candle_timestamp is not None else None)
                        )
                        if guard_intent:
                            events.append((trade["id"], "STOPPED_OUT_AT_BE"))
                        await self._record_trade_update(trade, None, None, price_event_ts)
                        continue
                    trade["state"] = "CLOSED"
                    opt_ctx["phase"] = "CLOSED"
                    opt_ctx["exit"] = {"reason": "STOPPED_OUT_AT_BE", "stop_domain": "SPOT_INDEX", "event_at": price_event_ts,
                                       "detected_at": datetime.now(timezone.utc).isoformat(),
                                       "received_at": event_ts_base,
                                       "index_price": trade["entry_price"], "exited_fraction": 0.5}
                    opt_ctx["remaining_fraction"] = 0.0
                    opt_ctx["index_guard_enabled"] = False
                    update_type = "STOPPED_OUT_AT_BE"
                    event_price = trade["entry_price"]
                    if reference_accounting:
                        self._capture_reference_leg(trade, "EXIT", event_price, price_event_ts, candle_timestamp)
                    self._mark_option_execution_route_pending(trade, update_type)
                    events.append((trade["id"], update_type))
                    await self._record_trade_update(trade, event_price, update_type, transition_event_ts)
                continue

            # Pre-T1 touch evaluations (trade state == "OPEN")
            target_1_hit = trade_high >= trade["target_1"] if sign == 1 else trade_low <= trade["target_1"]
            target_2_hit = trade_high >= trade["target_2"] if sign == 1 else trade_low <= trade["target_2"]
            stop_hit = trade_low <= trade["stop_loss"] if sign == 1 else trade_high >= trade["stop_loss"]

            if target_1_hit:
                trade["state"] = "T1_HIT"
                trade["stop_loss"] = trade["entry_price"]
                if candle_timestamp is not None:
                    activated = parse_candle_timestamp(candle_timestamp) + timedelta(minutes=1)
                elif event_time is not None or source_event_ts is not None:
                    activated = parse_candle_timestamp(source_event_ts or event_ts_base)
                else:
                    activated = datetime.now(timezone.utc)
                if not is_option_atr:
                    trade.setdefault("market_context", {})["atr_trailing"] = {
                        "activated_at": activated.isoformat(),
                        "last_bar": activated.isoformat(),
                    }
                if is_option_atr:
                    opt_ctx["phase"] = "POST_T1"
                    opt_ctx["pending_t1_cost_quotes"] = []
                    opt_ctx["highest_milestone"] = 1
                    if candle_timestamp is None and source_event_ts is not None:
                        t1_event_at = source_event_ts
                        opt_ctx["t1_activation_source"] = "tick_exchange"
                    else:
                        t1_event_at = event_ts
                        opt_ctx["t1_activation_source"] = (
                            "completed_candle" if candle_timestamp is not None else "tick_receipt"
                        )
                    opt_ctx["t1_at"] = t1_event_at
                    opt_ctx["booked_fraction"] = 0.5
                    opt_ctx["remaining_fraction"] = 0.5
                    opt_ctx["index_guard_enabled"] = True
                    opt_ctx["index_guard_stop"] = trade["entry_price"]
                    if opt_ctx.get("policy_version") == "option_premium_protection_v2":
                        entry_premium = float(opt_ctx["original_option_entry"])
                        old_stop = float(opt_ctx.get("locked_option_sl") or opt_ctx["original_option_sl"])
                        new_stop = max(old_stop, entry_premium)
                        old_source = opt_ctx.get("premium_stop_source")
                        next_source = ("ATR_TRAILING" if old_source == "ATR_TRAILING" and old_stop > entry_premium
                                       else old_source or "INITIAL_OPTION_SL" if old_stop > entry_premium
                                       else "BREAK_EVEN_COST")
                        opt_ctx.update(
                            previous_locked_option_sl=old_stop,
                            locked_option_sl=new_stop,
                            protection_mode="ATR" if next_source == "ATR_TRAILING" else next_source,
                            premium_stop_source=next_source,
                            stop_effective_at=t1_event_at,
                        )
                        if opt_ctx.get("atr_enabled"):
                            self._seed_option_stop(trade, parse_candle_timestamp(t1_event_at))
                    else:
                        self._seed_option_stop(trade, parse_candle_timestamp(t1_event_at))

                milestone_event_at = t1_event_at if is_option_atr else event_ts
                milestone_source = (
                    "completed_candle_touch" if candle_timestamp is not None else
                    "spot_tick_exchange_time" if market_event_time is not None else
                    "spot_tick_receipt_time" if event_time is not None else
                    "spot_observation"
                )
                milestone_time_basis = (
                    "candle_start" if candle_timestamp is not None else
                    "exchange_time" if market_event_time is not None else
                    "receipt_time" if event_time is not None else
                    "spot_observation"
                )
                self._queue_option_reference_milestone(
                    trade, "T1", milestone_event_at, trade["target_1"],
                    None if candle_timestamp is not None else spot_price,
                    source=milestone_source, time_basis=milestone_time_basis,
                )

                update_type = "T1_HIT"
                event_price = trade["target_1"]
                guard_route_intent = False
                if reference_accounting:
                    self._capture_reference_leg(trade, "T1", event_price, t1_event_at, candle_timestamp)
                    guard_touched = trade_low <= trade["entry_price"] if sign == 1 else trade_high >= trade["entry_price"]
                    if candle_timestamp is not None and guard_touched:
                        opt_ctx["ambiguous_index_touch"] = {
                            "kind": "T2_ENTRY" if target_2_hit else "T1_ENTRY",
                            "source_minute": candle_timestamp.isoformat(), "event_at": price_event_ts,
                            "high": trade_high, "low": trade_low, "status": "ORDER_UNKNOWN",
                        }
                        close_guard_touch = (spot_price <= trade["entry_price"] if sign == 1
                                             else spot_price >= trade["entry_price"])
                        guard_route_intent = (
                            opt_ctx.get("policy_version") == "option_premium_protection_v2"
                            and close_guard_touch
                            and self._retain_index_guard_execution_route(
                                trade, price_event_ts, event_ts_base,
                                index_watermark_checked=True,
                                prior_index_watermark=previous_index,
                                allow_t1_boundary=True,
                            )
                        )
                events.append((trade["id"], update_type))
                if guard_route_intent:
                    events.append((trade["id"], "STOPPED_OUT_AT_BE"))
                await self._record_trade_update(trade, event_price, update_type, transition_event_ts)
                if allow_t2_milestone and not is_option_atr and trade["state"] == "T1_HIT" and await self._record_t2_touch(
                    trade, trade_high, trade_low, event_ts_base, candle_timestamp, spot_price
                ):
                    events.append((trade["id"], "T2_TOUCHED"))

                # Target-first: If single index observation established both T1 and T2, apply T1 then T2 exactly once
                if (target_2_hit and is_option_atr
                        and not (opt_ctx.get("policy_version") == "option_premium_protection_v2"
                                 and self._unresolved_t1_premium_ambiguity(opt_ctx))
                        and (not reference_accounting or candle_timestamp is None or not (
                            trade_low <= trade["entry_price"] if sign == 1 else trade_high >= trade["entry_price"]))):
                    trade["state"] = "T2_REACHED"
                    opt_ctx["phase"] = "POST_T2"
                    opt_ctx["highest_milestone"] = 2
                    opt_ctx["t2_at"] = price_event_ts
                    opt_ctx["booked_fraction"] = 0.8
                    opt_ctx["remaining_fraction"] = 0.2
                    opt_ctx["index_guard_enabled"] = False
                    opt_ctx["observed_index_guard_exit"] = None
                    if opt_ctx.get("locked_option_sl") is None:
                        opt_ctx["locked_option_sl"] = opt_ctx.get("original_option_sl")
                        opt_ctx["protection_mode"] = "ORIGINAL_OPTION_SL_FLOOR"
                    update_type_t2 = "T2_HIT"
                    event_price_t2 = trade["target_2"]
                    self._queue_option_reference_target_touch(
                        trade, "T2", price_event_ts, trade["target_2"], spot_price, candle_timestamp,
                    )
                    if reference_accounting:
                        self._capture_reference_leg(trade, "T2", event_price_t2, price_event_ts, candle_timestamp)
                    events.append((trade["id"], update_type_t2))
                    await self._record_trade_update(trade, event_price_t2, update_type_t2, transition_event_ts)
                continue

            elif stop_hit:
                trade["state"] = "CLOSED"
                if is_option_atr:
                    opt_ctx["phase"] = "CLOSED"
                    opt_ctx["highest_milestone"] = 0
                    opt_ctx["booked_fraction"] = 0.0
                    opt_ctx["remaining_fraction"] = 0.0
                    opt_ctx["index_guard_enabled"] = False
                    opt_ctx["exit"] = {"reason": "SL_HIT", "stop_domain": "SPOT_INDEX", "event_at": price_event_ts,
                                       "detected_at": datetime.now(timezone.utc).isoformat(),
                                       "received_at": event_ts_base,
                                       "index_price": trade["stop_loss"], "exited_fraction": 1.0}

                update_type = "SL_HIT"
                event_price = trade["stop_loss"]
                if reference_accounting:
                    self._capture_reference_leg(trade, "EXIT", event_price, price_event_ts, candle_timestamp)
                self._mark_option_execution_route_pending(trade, update_type)
                events.append((trade["id"], update_type))
                await self._record_trade_update(trade, event_price, update_type, transition_event_ts)
                continue

        return events

    async def _record_t2_touch(self, trade, high, low, event_ts, candle_timestamp, spot_price):
        """Record one spot milestone without changing the ATR runner lifecycle."""
        target = trade.get("target_2")
        context = trade.get("market_context") or {}
        # New spot-ATR registrations opt in at creation. Older trades need an
        # explicit per-trade review marker before enabling this notification.
        if context.get("legacy_t2_tracking_approved") is not True:
            return False
        if target is None or (context.get("target_milestones") or {}).get("t2"):
            return False
        time_basis = "candle_start" if candle_timestamp is not None else "spot_observation"
        if candle_timestamp is None and trade.get("entry_timestamp"):
            if parse_candle_timestamp(event_ts) < parse_candle_timestamp(trade["entry_timestamp"]):
                return False
        if candle_timestamp is not None and trade.get("entry_timestamp"):
            source_at = parse_candle_timestamp(candle_timestamp)
            entry_at = parse_candle_timestamp(trade["entry_timestamp"])
            if source_at + timedelta(minutes=1) <= entry_at:
                return False
            if source_at <= entry_at:
                high = low = spot_price
                event_ts = __import__("storage").to_utc_iso(entry_at)
                time_basis = "entry_boundary"
        touched = high >= target if trade["direction"] == "BULLISH" else low <= target
        if not touched:
            return False
        sign = 1 if trade["direction"] == "BULLISH" else -1
        marker = {"touched_at": event_ts, "time_basis": time_basis, "target_spot": target,
                  "spot_points_from_entry": round(sign * (float(target) - float(trade["entry_price"])), 2)}
        trade.setdefault("market_context", {}).setdefault("target_milestones", {})["t2"] = marker
        self._queue_option_reference_milestone(
            trade, "T2", event_ts, target, spot_price,
            source="completed_candle_touch" if candle_timestamp is not None else "spot_observation",
            time_basis=time_basis,
        )
        self.__dict__.setdefault("_pending_target_milestones", {})[trade["id"]] = deepcopy(marker)
        reference_snapshot = deepcopy((trade.get("market_context") or {}).get("option_reference_milestones"))
        self._queue_trade_write(
            trade["id"],
            lambda t_id=trade["id"], snapshot=deepcopy(marker), refs=reference_snapshot:
                self._persist_trade_context(t_id, t2_snapshot=snapshot,
                                            reference_milestone_snapshot=refs),
        )
        self._queue_trade_alert(trade, target, "T2_TOUCHED")
        return True

    @staticmethod
    def _validated_option_reference_contract(sizing, entry_timestamp):
        """Validate a one-lot report pin without consulting bracket execution fields."""
        try:
            security_id = str(sizing.get("security_id") or "").strip()
            expiry = datetime.strptime(str(sizing.get("expiry_date") or ""), "%Y-%m-%d").date()
            event_at = parse_candle_timestamp(entry_timestamp)
            strike_raw = sizing.get("strike")
            lot_raw = sizing.get("lot_size")
            if isinstance(strike_raw, bool) or isinstance(lot_raw, bool):
                return None
            strike, lot_size = float(strike_raw), float(lot_raw)
            option_type = str(sizing.get("option_type") or "").upper()
        except (TypeError, ValueError, OverflowError, OSError):
            return None
        if (not security_id.isdigit() or int(security_id) <= 0
                or not isfinite(strike) or strike <= 0 or not strike.is_integer()
                or not isfinite(lot_size) or lot_size <= 0 or not lot_size.is_integer()
                or expiry < event_at.date() or option_type not in {"CE", "PE"}):
            return None
        return {
            "security_id": security_id,
            "exchange_segment": "NSE_FNO",
            "expiry_date": expiry.isoformat(),
            "strike": int(strike),
            "option_type": option_type,
            "lot_size": int(lot_size),
        }

    def _queue_option_reference_milestone(
        self, trade, name, event_at, spot_target, spot_price, *, source, time_basis,
    ):
        """Queue a spot-only T1/T2 marker for the independent background worker."""
        context = trade.get("market_context") or {}
        reference = context.get("option_reference")
        if (not isinstance(reference, dict)
                or reference.get("formula_version") != "one_lot_hold_to_exit_v1"
                or name not in {"T1", "T2"}):
            return False
        try:
            event = parse_candle_timestamp(event_at).isoformat()
            target = float(spot_target)
            price = float(spot_price) if spot_price is not None else None
        except (TypeError, ValueError, OverflowError, OSError):
            return False
        if not isfinite(target) or target <= 0 or (price is not None and (not isfinite(price) or price <= 0)):
            return False
        milestones = reference.setdefault("milestones", {})
        marker = milestones.get(name)
        is_new = not isinstance(marker, dict)
        if is_new:
            marker = {
                "event_at": event,
                "spot_target": target,
                "spot_price": price,
                "source": source,
                "time_basis": time_basis,
            }
            milestones[name] = marker
        context.setdefault("option_reference_milestones", {})[name] = deepcopy(marker)
        self.__dict__.setdefault("_pending_option_reference_context", {})[str(trade["id"])] = deepcopy(
            context["option_reference_milestones"]
        )
        if not is_new:
            return False
        pending = self.__dict__.setdefault("_pending_option_reference_events", {})
        events = pending.setdefault(str(trade["id"]), [])
        events.append({"kind": "MILESTONE", "name": name, "milestone": deepcopy(marker)})
        return True

    def _queue_option_reference_target_touch(self, trade, name, event_at, target, spot_price, candle_timestamp):
        """Use shared timestamp/source labels for target touches from a spot observation."""
        from_candle = candle_timestamp is not None
        return self._queue_option_reference_milestone(
            trade, name, event_at, target, None if from_candle else spot_price,
            source="completed_candle_touch" if from_candle else "spot_observation",
            time_basis="candle_start" if from_candle else "spot_observation",
        )

    def _persist_trade_context(
        self, trade_id, atr_snapshot=None, t2_snapshot=None, target_snapshot=None,
        reference_milestone_snapshot=None,
    ):
        """Merge owned context keys under the shared execution/ATR context lock."""
        if (atr_snapshot is None and t2_snapshot is None and target_snapshot is None
                and reference_milestone_snapshot is None):
            return
        with self._context_locks.setdefault(trade_id, threading.Lock()):
            response = self.supabase.table("trade_analytics").select("market_context").eq("id", trade_id).execute()
            if not response.data:
                raise RuntimeError("ATR trade analytics row is unavailable")
            context = dict(response.data[0].get("market_context") or {})
            if atr_snapshot is not None:
                context["atr_trailing"] = atr_snapshot
            if isinstance(target_snapshot, dict):
                context["target_milestones"] = {
                    **(context.get("target_milestones") or {}), **target_snapshot,
                }
            if t2_snapshot is not None:
                context["target_milestones"] = {
                    **(context.get("target_milestones") or {}), "t2": t2_snapshot,
                }
            if isinstance(reference_milestone_snapshot, dict) and reference_milestone_snapshot:
                context["option_reference_milestones"] = {
                    **(context.get("option_reference_milestones") or {}),
                    **reference_milestone_snapshot,
                }
            updated = self.supabase.table("trade_analytics").update({"market_context": context}).eq("id", trade_id).execute()
            if t2_snapshot is not None:
                if not updated.data:
                    raise RuntimeError("T2 milestone update was not persisted")
                if self._pending_target_milestones.get(trade_id) == t2_snapshot:
                    self._pending_target_milestones.pop(trade_id, None)
            if isinstance(reference_milestone_snapshot, dict) and reference_milestone_snapshot:
                if not updated.data:
                    raise RuntimeError("Option reference milestone context was not persisted")
                pending = getattr(self, "_pending_option_reference_context", {})
                if pending.get(str(trade_id)) == reference_milestone_snapshot:
                    pending.pop(str(trade_id), None)


    async def _record_trade_update(self, trade, event_price, update_type, event_ts, pnl_points_override=None):
        """Persist each lifecycle transition through the existing ordered writer."""
        terminal = trade.get("state") in ["CLOSED", "STOPPED_OUT"]
        if terminal:
            self._cleanup_option_selection_if_terminal()
        opt = (trade.get("market_context") or {}).get("option_trailing")
        if _has_option_policy(opt):
            await self._record_option_transition(trade, event_price, update_type, event_ts)
            target_snapshot = deepcopy((trade.get("market_context") or {}).get("target_milestones")) or None
            reference_snapshot = deepcopy(
                (trade.get("market_context") or {}).get("option_reference_milestones")
            ) or None
            if target_snapshot or reference_snapshot:
                self._queue_trade_write(
                    trade["id"],
                    lambda t_id=trade["id"], snapshot=target_snapshot, refs=reference_snapshot:
                        self._persist_trade_context(t_id, target_snapshot=snapshot,
                                                    reference_milestone_snapshot=refs),
                )
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
        t2_snapshot = deepcopy((trade.get("market_context", {}).get("target_milestones") or {}).get("t2"))
        target_snapshot = deepcopy(trade.get("market_context", {}).get("target_milestones")) or None
        reference_snapshot = deepcopy(
            trade.get("market_context", {}).get("option_reference_milestones")
        ) or None

        opt_trailing = trade.get("market_context", {}).get("option_trailing")
        opt_snapshot = deepcopy(opt_trailing) if opt_trailing else None

        def _update(t_id=trade["id"], data=update_data, snapshot=atr_snapshot,
                    milestone=t2_snapshot, target=target_snapshot, refs=reference_snapshot):
            terminal = data["state"] in ["CLOSED", "STOPPED_OUT"]
            if terminal:
                self.supabase.table("active_trades").update(data).eq("id", t_id).execute()
                self._cleanup_option_selection_if_terminal()
            self._persist_trade_context(t_id, snapshot, milestone, target, refs)
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
            if trade["id"] in getattr(self, "_trade_alerts", {}):
                self._queue_trade_alert(trade, event_price, update_type)
            else:
                try:
                    await send_trade_update(trade, event_price, update_type)
                except Exception as e:
                    print(f"Failed to send trade update alert: {e}")

    async def update_atr_trailing(self, candles):
        """Ratchet post-T1 stops and exit at completed five-minute closes only."""
        candles = list(candles or ())
        bars = completed_atr_bars(candles)
        events = []
        source_minutes = sorted((at, values) for at, values in completed_spot_minutes(candles).items()
                                if values is not None)
        if not source_minutes:
            return events
        for trade in self.active_trades:
            # Skip trades governed by option premium ATR trailing
            if _is_option_instrument(trade):
                continue
            if trade["state"] != "T1_HIT":
                continue
            context = trade.setdefault("market_context", {})
            atr = context.setdefault("atr_trailing", {})
            try:
                watermark = parse_candle_timestamp(atr["last_bar"]) if atr.get("last_bar") else None
            except (TypeError, ValueError, OverflowError, OSError):
                watermark = None
            now = datetime.now(timezone.utc)
            if watermark is None or watermark > now + timedelta(seconds=5):
                activation_value = atr.get("activated_at")
                if not activation_value:
                    activation_value = (((context.get("option_reference") or {}).get("milestones") or {})
                                        .get("T1", {}).get("event_at"))
                try:
                    activation = parse_candle_timestamp(activation_value) if activation_value else None
                except (TypeError, ValueError, OverflowError, OSError):
                    activation = None
                if activation is not None and activation <= now + timedelta(seconds=5):
                    atr["activated_at"] = activation.isoformat()
                    try:
                        entry_stop = float(trade["entry_price"])
                        active_stop = float(trade["stop_loss"])
                        can_replay_from_t1 = isfinite(entry_stop) and active_stop == entry_stop
                    except (KeyError, TypeError, ValueError, OverflowError):
                        can_replay_from_t1 = False
                    if not can_replay_from_t1:
                        # A ratcheted live stop has no recoverable per-bar history
                        # once its watermark is corrupt; preserve it and resume prospectively.
                        if bars:
                            atr["last_bar"] = bars[-1]["timestamp"]
                            await self._record_trade_update(
                                trade, bars[-1]["close"], None, bars[-1]["timestamp"])
                        continue
                    atr["last_bar"] = activation.isoformat()
                    watermark = activation
                elif bars:
                    # Legacy runners without a T1 anchor resume at trusted history.
                    atr["last_bar"] = bars[-1]["timestamp"]
                    await self._record_trade_update(trade, bars[-1]["close"], None, bars[-1]["timestamp"])
                    continue
                else:
                    continue
            sign = 1 if trade["direction"] == "BULLISH" else -1
            eligible = [(at, values) for at, values in source_minutes if at + timedelta(minutes=1) > watermark]
            cursor = 0
            for bar in [*bars, None]:
                bar_end = parse_candle_timestamp(bar["timestamp"]) if bar is not None else None
                if bar_end is not None and bar_end <= watermark:
                    continue
                # Check every valid source minute, even when an ATR group has a
                # gap, before processing any later five-minute close.
                while cursor < len(eligible) and (bar_end is None or eligible[cursor][0] + timedelta(minutes=1) <= bar_end):
                    source_at, (high, low, close) = eligible[cursor]
                    cursor += 1
                    if await self._record_t2_touch(
                        trade, high, low, __import__("storage").to_utc_iso(source_at), source_at, close,
                    ):
                        events.append((trade["id"], "T2_TOUCHED"))
                if bar is None:
                    break
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
                              ("_option_warning_sent", set), ("_option_confirmed_sessions", set),
                              ("_option_flush_tasks", dict), ("_option_flush_followups", set)):
            if not hasattr(self, name):
                setattr(self, name, factory())

    def set_reference_price_evidence(self, *, option_minutes=None, option_quotes=None,
                                     spot_minutes=None, spot_quotes=None):
        """Cache sampled evidence; callers fetch outside the protective path."""
        cache = getattr(self, "_reference_price_evidence", {})
        for key, value in (("option_minutes", option_minutes), ("option_quotes", option_quotes),
                           ("spot_minutes", spot_minutes), ("spot_quotes", spot_quotes)):
            if value is not None:
                cache[key] = deepcopy(value)
        self._reference_price_evidence = cache

    def record_reference_spot_quote(self, price, observed_at, source):
        """Keep a bounded cache of timestamped live spot quotes for exit recovery."""
        try:
            value = float(price)
            at = parse_candle_timestamp(observed_at)
        except (TypeError, ValueError, OverflowError, OSError):
            return False
        if not isfinite(value) or value <= 0 or not isinstance(source, str) or not source.strip():
            return False
        cache = getattr(self, "_reference_price_evidence", {})
        quotes = list(cache.get("spot_quotes") or [])
        quote = {"price": value, "observed_at": at.isoformat(), "source": source.strip()}
        if quote not in quotes:
            quotes.append(quote)
        quotes.sort(key=lambda item: parse_candle_timestamp(item["observed_at"]))
        cache["spot_quotes"] = quotes[-1000:]
        self._reference_price_evidence = cache
        try:
            from quote_service import get_quote_service
            get_quote_service().record_spot_quote(value, at, source=source.strip())
        except Exception:
            pass
        return True

    def get_selected_option_contract(self) -> Optional[dict]:
        """Return the strictly validated option contract for the active trade, or None."""
        from quote_service import validate_option_contract
        for trade in self.active_trades:
            if trade.get("state") in {"CLOSED", "STOPPED_OUT"}:
                continue
            context = trade.get("market_context") or {}
            sizing = context.get("options_sizing") or {}
            validated = validate_option_contract(sizing)
            if validated:
                return validated
            ref = (context.get("option_reference") or {}).get("contract") or {}
            validated_ref = validate_option_contract(ref)
            if validated_ref:
                return validated_ref
            trailing = (context.get("option_trailing") or {}).get("contract") or {}
            validated_trailing = validate_option_contract(trailing)
            if validated_trailing:
                return validated_trailing
        return None

    def _cleanup_option_selection_if_terminal(self) -> None:
        """Reconcile the selected option quote with current active trades.

        If no active trades remain, clear the selected option quote.
        If active trades remain but the selected contract differs from the cached quote,
        clear the cached quote so closed contracts do not continue being persisted or served.
        """
        try:
            from quote_service import get_quote_service
            qs = get_quote_service()
            selected = self.get_selected_option_contract()
            cleared = False
            if hasattr(qs, "reconcile_selected_contract"):
                cleared = qs.reconcile_selected_contract(selected)
            elif selected is None:
                qs.clear_selected_option()
                cleared = True
            else:
                cached = getattr(qs, "get_cached_option_contract", lambda: None)()
                if cached is not None:
                    from quote_service import are_option_contracts_equal
                    if not are_option_contracts_equal(cached, selected):
                        qs.clear_selected_option()
                        cleared = True
            if cleared:
                try:
                    import asyncio
                    loop = asyncio.get_running_loop()
                    if loop.is_running():
                        loop.create_task(qs.flush(force=True))
                except RuntimeError:
                    pass
        except Exception:
            pass

    @staticmethod
    def _reference_totals(trade):
        opt = trade["market_context"]["option_trailing"]
        accounting = opt.setdefault("accounting", {"legs": {}})
        legs = accounting.setdefault("legs", {})
        highest = opt.get("highest_milestone", 0)
        sign = 1 if trade["direction"] == "BULLISH" else -1
        def total(market, include_exit):
            entry = trade.get("entry_price") if market == "spot" else opt.get("original_option_entry")
            names = [("T1", .5)] if highest >= 1 else []
            if highest >= 2:
                names.append(("T2", .3))
            if include_exit:
                names.append(("EXIT", 1.0 if highest == 0 else .5 if highest == 1 else .2))
            if entry is None or any(legs.get(name, {}).get(market + "_price") is None for name, _ in names):
                return None
            direction = sign if market == "spot" else 1
            return round(sum(weight * direction * (float(legs[name][market + "_price"]) - float(entry))
                             for name, weight in names), 8)
        for market in ("spot", "option"):
            accounting[market + "_booked_points"] = total(market, False)
            accounting[market + "_total_points"] = total(market, True) if opt.get("phase") == "CLOSED" else None
        return accounting

    @staticmethod
    def _merge_reference_evidence(committed, observed):
        """Combine disjoint immutable leg evidence without erasing either writer."""
        merged = deepcopy(committed or {})
        legs = merged.setdefault("legs", {})
        for name, leg in ((observed or {}).get("legs") or {}).items():
            target = legs.setdefault(name, {})
            for key, value in leg.items():
                if value is None:
                    continue
                if target.get(key) is not None and target[key] != value:
                    raise RuntimeError(f"Conflicting committed reference evidence: {name}.{key}")
                target[key] = deepcopy(value)
        return merged

    def _capture_reference_leg(self, trade, name, spot_price, event_at, source_minute=None, *, option_close=None):
        opt = trade["market_context"]["option_trailing"]
        leg = opt.setdefault("accounting", {}).setdefault("legs", {}).setdefault(name, {})
        leg.update(event_at=event_at if source_minute is None else None,
                   source_minute=source_minute.isoformat() if source_minute else None,
                   spot_price=spot_price, spot_source="touched_level" if spot_price is not None else None)
        if option_close is not None:
            if spot_price is not None:
                leg["spot_source"] = "completed_1m_close"
                leg["spot_observed_at"] = event_at
                leg["spot_approximate"] = True
            leg.update(option_price=option_close, option_source="completed_5m_close",
                       option_observed_at=event_at, approximate=False)
        self._enrich_reference_trade(trade)

    def _enrich_reference_trade(self, trade):
        opt = trade["market_context"]["option_trailing"]
        legs = opt.setdefault("accounting", {}).setdefault("legs", {})
        cache = getattr(self, "_reference_price_evidence", {})
        security = str((opt.get("contract") or {}).get("security_id", ""))
        minutes = cache.get("option_minutes", {}).get(security, [])
        quotes = cache.get("option_quotes", {}).get(security, [])
        for name, leg in legs.items():
            if leg.get("option_price") is None:
                evidence = option_target_evidence(leg.get("event_at"), observations=quotes,
                    option_minutes=minutes, source_minute=leg.get("source_minute"))
                if evidence:
                    leg.update(option_price=evidence["price"], option_source=evidence["source"],
                               option_observed_at=evidence["observed_at"], approximate=evidence["approximate"])
            if (name == "EXIT" and leg.get("spot_price") is None
                    and (leg.get("option_source") == "completed_5m_close" or is_premium_trigger_exit(opt, leg))):
                evidence = spot_exit_evidence(leg.get("event_at"), spot_minutes=cache.get("spot_minutes", []),
                                              observations=cache.get("spot_quotes", []),
                                              allow_intraminute=is_premium_trigger_exit(opt, leg))
                if evidence:
                    leg.update(spot_price=evidence["price"], spot_source=evidence["source"],
                               spot_observed_at=evidence["observed_at"], spot_approximate=evidence["approximate"])
        return self._reference_totals(trade)

    async def enrich_reference_accounting(self):
        """Persist newly available evidence on open trades through the ordered writer."""
        for trade in list(self.active_trades):
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            if (not _has_option_policy(opt)
                    or opt.get("accounting_formula_version") != "one_lot_reference_v1"
                    or trade.get("state") in {"CLOSED", "STOPPED_OUT"}):
                continue
            before = deepcopy(opt.get("accounting"))
            self._enrich_reference_trade(trade)
            if before != opt.get("accounting"):
                await self._record_option_transition(trade, None, None, datetime.now(timezone.utc).isoformat())

    async def resolve_reference_ambiguities_from_feed(self, tick_feed):
        """Resolve only when a feed explicitly certifies complete ordered history."""
        get_history = getattr(tick_feed, "get_complete_minute_observations", None)
        if not callable(get_history):
            return 0
        resolved = 0
        for trade in list(self.active_trades):
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            ambiguity = opt.get("ambiguous_index_touch")
            if not ambiguity or not ambiguity.get("source_minute"):
                continue
            observations, complete = get_history(ambiguity["source_minute"])
            if complete is True and await self.resolve_reference_ambiguity(
                    trade["id"], observations, complete=True):
                resolved += 1
        return resolved

    async def resolve_reference_ambiguity(self, trade_id, observations, *, complete=False):
        """Resolve only with a certified complete, ordered tick history of the source minute.

        Each observation has an observed_at, price and nonempty source. A caller
        must only certify complete when the provider guarantees tick coverage;
        ordinary quotes and OHLC candles are not complete history.
        """
        if complete is not True:
            return False
        trade = next((t for t in self.active_trades if t.get("id") == trade_id), None)
        if trade is None or trade.get("state") != "T1_HIT":
            return False
        opt = trade["market_context"]["option_trailing"]
        if opt.get("accounting_formula_version") != "one_lot_reference_v1":
            return False
        ambiguity = opt.get("ambiguous_index_touch")
        if not ambiguity or not ambiguity.get("source_minute"):
            return False
        if ambiguity.get("kind") == "T1_PHASE_ENTRY" and opt.get("t1_activation_source") != "tick_exchange":
            # A completed-candle boundary has no ordered tick representing its
            # close, so second-resolution ticks cannot prove which phase came first.
            return False
        if ambiguity.get("kind") not in {"T1_ENTRY", "T2_ENTRY", "T1_PHASE_ENTRY"}:
            return False
        if self._unresolved_t1_premium_ambiguity(opt):
            # Certified index ticks establish index-touch order only. They do
            # not order an option quote/bar against those touches at T1.
            return False
        start = parse_candle_timestamp(ambiguity["source_minute"])
        end = start + timedelta(minutes=1)
        ticks = []
        try:
            for item in observations:
                at = parse_candle_timestamp(item["observed_at"])
                price = float(item["price"])
                if not start <= at < end or not isfinite(price) or price <= 0 or not str(item.get("source") or "").strip():
                    return False
                ticks.append((at, price))
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        # Equal-second ordering is accepted only when the caller supplies
        # certified complete, ordered source history; local arrival order alone
        # cannot establish it.
        if not ticks or any(ticks[i][0] > ticks[i + 1][0] for i in range(len(ticks) - 1)):
            return False
        sign = 1 if trade["direction"] == "BULLISH" else -1
        # Prove the threshold touches stated by the source candle.
        t1_entry_only = ambiguity.get("kind") in {"T1_ENTRY", "T1_PHASE_ENTRY"}
        target = trade["target_1"] if t1_entry_only else trade["target_2"]
        if not any(sign * (price - target) >= 0 for _, price in ticks):
            return False
        if not any(sign * (price - trade["entry_price"]) <= 0 for _, price in ticks):
            return False
        t1_at = parse_candle_timestamp(opt["t1_at"]) if opt.get("t1_at") else None
        t1_same_candle = t1_at is not None and start <= t1_at <= end
        t1_seen = not t1_same_candle
        decision = None
        for at, price in ticks:
            if not t1_seen:
                t1_seen = sign * (price - trade["target_1"]) >= 0
                if not t1_seen:
                    continue
            if sign * (price - trade["target_2"]) >= 0:
                decision = ("T2_HIT", at)
                break
            if sign * (price - trade["entry_price"]) <= 0:
                decision = ("STOPPED_OUT_AT_BE", at)
                break
        if decision is None:
            if not t1_entry_only or not t1_seen:
                return False
            # Resolve the first source minute, then select the earliest retained
            # spot touch or premium exit. A tie cannot establish market order.
            subsequent = deepcopy(ambiguity.get("subsequent_index_observations") or [])
            for observation in opt.get("pending_index_observations") or []:
                if observation not in subsequent:
                    subsequent.append(deepcopy(observation))
            try:
                subsequent.sort(key=lambda item: parse_candle_timestamp(item["event_at"]))
                if any(parse_candle_timestamp(item["event_at"]) <= end for item in subsequent):
                    return False
                if any(parse_candle_timestamp(subsequent[i]["event_at"]) ==
                       parse_candle_timestamp(subsequent[i + 1]["event_at"])
                       for i in range(len(subsequent) - 1)):
                    return False
                later_spot = subsequent[0] if subsequent else None
                protective = ambiguity.get("protective_option_exit")
                protective_at = parse_candle_timestamp(protective["event_at"]) if protective else None
                spot_at = parse_candle_timestamp(later_spot["event_at"]) if later_spot else None
            except (KeyError, TypeError, ValueError, OverflowError):
                return False
            if spot_at is not None and protective_at is not None and spot_at == protective_at:
                return False
            if protective_at is not None and (spot_at is None or protective_at < spot_at):
                decision = ("OPTION_EXIT", protective_at)
            elif later_spot:
                high, low = later_spot.get("high"), later_spot.get("low")
                if high is None or low is None:
                    return False
                touches_t2 = sign * ((high if sign == 1 else low) - trade["target_2"]) >= 0
                touches_guard = sign * ((low if sign == 1 else high) - trade["entry_price"]) <= 0
                if touches_t2 and touches_guard:
                    source_minute = later_spot.get("candle_timestamp")
                    if not source_minute:
                        return False
                    ambiguity.update(kind="T2_ENTRY", source_minute=source_minute,
                        event_at=later_spot["event_at"], high=high, low=low,
                        subsequent_index_observations=subsequent[1:])
                    opt["pending_index_observations"] = []
                    await self._record_trade_update(trade, None, None, later_spot["event_at"])
                    return False
                if not touches_t2 and not touches_guard:
                    return False
                decision = ("T2_HIT" if touches_t2 else "STOPPED_OUT_AT_BE", spot_at)
            else:
                decision = ("T1_ONLY", end)
        protective = ambiguity.get("protective_option_exit")
        if protective and decision[0] in {"T2_HIT", "STOPPED_OUT_AT_BE"}:
            try:
                protective_at = parse_candle_timestamp(protective["event_at"])
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                return False
            if protective_at == decision[1]:
                return False
            if protective_at < decision[1]:
                decision = ("OPTION_EXIT", protective_at)
        kind, at = decision
        stamp = __import__("storage").to_utc_iso(at)
        opt["ambiguity_resolution"] = {"source_minute": ambiguity["source_minute"],
            "event_at": stamp, "outcome": kind, "evidence_source": observations[0]["source"]}
        # The SQL transition merges JSONB objects. An explicit JSON null is
        # required to clear the durable marker across restart.
        opt["ambiguous_index_touch"] = None
        if kind == "T1_ONLY":
            await self._record_trade_update(trade, None, None, stamp)
            return True
        if t1_entry_only:
            opt["pending_index_observations"] = []
        if kind == "OPTION_EXIT":
            spot = self._finalize_protective_option_exit(trade, protective, exited_fraction=.5)
            await self._record_trade_update(trade, spot, protective["reason"], protective["event_at"])
            return True
        if kind == "STOPPED_OUT_AT_BE":
            trade["state"] = "CLOSED"
            opt.update(phase="CLOSED", remaining_fraction=0.0, index_guard_enabled=False,
                exit={"reason": kind, "stop_domain": "SPOT_INDEX", "event_at": stamp,
                      "detected_at": datetime.now(timezone.utc).isoformat(),
                      "received_at": ambiguity.get("received_at"),
                      "index_price": trade["entry_price"], "exited_fraction": .5})
            source = (later_spot.get("candle_timestamp") if t1_entry_only and 'later_spot' in locals() and later_spot else None)
            self._capture_reference_leg(trade, "EXIT", trade["entry_price"], stamp,
                                        parse_candle_timestamp(source) if source else None)
            # The reference close is also an executable index-guard stop for a
            # confirmed v2 paper fill; the main monitor drains this durable intent.
            self._mark_option_execution_route_pending(trade, kind)
            await self._record_trade_update(trade, trade["entry_price"], kind, stamp)
            return True
        trade["state"] = "T2_REACHED"
        opt.update(phase="POST_T2", highest_milestone=2, t2_at=stamp,
                   booked_fraction=.8, remaining_fraction=.2, index_guard_enabled=False,
                   observed_index_guard_exit=None)
        if opt.get("locked_option_sl") is None:
            opt.update(locked_option_sl=opt["original_option_sl"], protection_mode="ORIGINAL_OPTION_SL_FLOOR")
        source = (later_spot.get("candle_timestamp") if t1_entry_only and 'later_spot' in locals() and later_spot else None)
        self._capture_reference_leg(trade, "T2", trade["target_2"], stamp,
                                    parse_candle_timestamp(source) if source else None)
        await self._record_trade_update(trade, trade["target_2"], kind, stamp)
        if protective:
            spot = self._finalize_protective_option_exit(trade, protective, exited_fraction=.2)
            await self._record_trade_update(trade, spot, protective["reason"], protective["event_at"])
        return True

    def _finalize_protective_option_exit(self, trade, protective, *, exited_fraction):
        """Finalize a deferred premium exit after earlier index ordering is known."""
        opt = trade["market_context"]["option_trailing"]
        reason = protective["reason"]
        price = float(protective["option_close"])
        event_at = protective["event_at"]
        stop = protective.get("effective_stop", protective.get("option_stop"))
        stop_source = protective.get("stop_source") or (
            "ATR_TRAILING" if reason == "ATR_SL_HIT" else
            "BREAK_EVEN_COST" if reason == "STOPPED_OUT_AT_BE" else "INITIAL_OPTION_SL")
        source = protective.get("source") or protective.get("trigger_source") or "completed_5m_option_close"
        opt.update(phase="CLOSED", remaining_fraction=0.0, index_guard_enabled=False,
                   monitoring_status="CLOSED")
        opt["deferred_option_exit"] = None
        opt["pending_index_observations"] = []
        opt["observed_index_guard_exit"] = None
        opt["exit_trigger"] = {
            "domain": "OPTION_PREMIUM", "source": source, "event_at": event_at,
            "received_at": protective.get("received_at"), "price": price,
            "effective_stop": stop, "stop_source": stop_source, "reason": reason,
        }
        for key in ("stop_ordering_status", "raised_stop"):
            if protective.get(key) is not None:
                opt["exit_trigger"][key] = protective[key]
        opt["execution_status"] = ("REFERENCE_TRIGGERED_NO_BROKER_FILL"
                                    if opt.get("execution_status") in {"REFERENCE_ONLY", "REFERENCE_TRIGGERED_NO_BROKER_FILL"}
                                    else "EXIT_TRIGGERED_PENDING_CONFIRMATION")
        runner_points = round(price - float(opt["original_option_entry"]), 8)
        opt["exit"] = {
            "reason": reason, "stop_domain": "OPTION_PREMIUM", "event_at": event_at,
            "detected_at": protective.get("detected_at") or datetime.now(timezone.utc).isoformat(),
            "received_at": protective.get("received_at"), "option_close": price,
            "reference_exit_price": price, "option_stop": stop, "stop_source": stop_source,
            "trigger_source": source, "trigger_price": protective.get("trigger_price", price),
            "exited_fraction": exited_fraction,
            "runner_option_points": 0.0 if reason == "STOPPED_OUT_AT_BE" else runner_points,
            "fill_price": None,
        }
        for key in ("stop_ordering_status", "raised_stop"):
            if protective.get(key) is not None:
                opt["exit"][key] = protective[key]
        trade["state"] = "CLOSED"
        if opt.get("accounting_formula_version") == "one_lot_reference_v1":
            self._capture_reference_leg(trade, "EXIT", None, event_at, option_close=price)
            leg = opt["accounting"]["legs"]["EXIT"]
            leg.update(option_source=source, option_observed_at=event_at, option_approximate=False,
                       trigger_price=opt["exit"]["trigger_price"], effective_stop=stop,
                       stop_source=stop_source)
            opt["exit"]["index_price"] = leg.get("spot_price")
        return opt.get("exit", {}).get("index_price")

    async def _apply_deferred_option_exit_at_event(self, event_at):
        """Finalize a retained premium trigger only after earlier index events replay."""
        event_at = parse_candle_timestamp(event_at)
        events = []
        for trade in self.active_trades:
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            deferred = opt.get("deferred_option_exit")
            if not deferred:
                continue
            try:
                deferred_at = parse_candle_timestamp(deferred["event_at"])
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                continue
            if deferred_at != event_at:
                continue
            if trade.get("state") in {"CLOSED", "STOPPED_OUT"}:
                # Terminal cleanup belongs to the original closure snapshot;
                # the lifecycle RPC rejects new transitions after closure.
                continue

            unresolved_prior_index = False
            for observation in opt.get("pending_index_observations") or []:
                try:
                    if parse_candle_timestamp(observation["event_at"]) <= deferred_at:
                        unresolved_prior_index = True
                        break
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    unresolved_prior_index = True
                    break
            if unresolved_prior_index:
                continue

            ambiguity = opt.get("ambiguous_index_touch")
            if ambiguity:
                try:
                    ambiguous_at = parse_candle_timestamp(ambiguity.get("event_at") or ambiguity["source_minute"])
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    ambiguous_at = deferred_at
                if ambiguous_at <= deferred_at:
                    ambiguity.setdefault("protective_option_exit", deepcopy(deferred))
                    opt["deferred_option_exit"] = None
                    await self._record_trade_update(trade, None, None, deferred_at.isoformat())
                    continue

            phase = opt.get("phase")
            if phase not in {"POST_T1", "POST_T2"}:
                continue
            t2_at = parse_candle_timestamp(opt["t2_at"]) if opt.get("t2_at") else None
            if phase == "POST_T2" and t2_at is not None and t2_at >= deferred_at:
                # A later or same-time T2 must not retroactively change the exit allocation.
                if deferred.get("ordering_status") != "ORDER_UNKNOWN":
                    deferred["ordering_status"] = "ORDER_UNKNOWN"
                    await self._record_trade_update(trade, None, None, deferred_at.isoformat())
                continue
            exited_fraction = float(opt.get("remaining_fraction", 0.0))
            if exited_fraction <= 0:
                continue
            protective = deepcopy(deferred)
            spot = self._finalize_protective_option_exit(
                trade, protective, exited_fraction=exited_fraction)
            await self._record_trade_update(trade, spot, protective["reason"], protective["event_at"])
            events.append((trade["id"], protective["reason"]))
        return events

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
            if opt.get("policy_version") == "option_premium_protection_v2":
                # An earlier recovered stop supersedes later deferred events.
                # Persist cleanup atomically with either premium or index exit.
                opt.update(deferred_option_exit=None, pending_index_observations=[],
                           observed_index_guard_exit=None)
            self._cleanup_option_selection_if_terminal()
        reference_accounting = opt.get("accounting_formula_version") == "one_lot_reference_v1"
        accounting = self._enrich_reference_trade(trade) if reference_accounting else {}
        snapshot = deepcopy(opt)
        # Market identity is stable across retries; it does not depend on a stale revision.
        identity = ":".join(str(v) for v in (trade["id"], update_type or "WATERMARK", event_ts,
                            snapshot.get("locked_option_sl"), snapshot.get("last_processed_option_bar_end")))
        quote = snapshot.get("last_option_quote")
        if isinstance(quote, dict):
            identity += ":quote:" + ":".join(str(quote.get(key) or "") for key in (
                "observed_at", "received_at", "price"))
        route_trigger = snapshot.get("execution_route_trigger")
        if isinstance(route_trigger, dict) and route_trigger:
            # An earlier watermark at the same source timestamp must not
            # make this newly observed protective route look like a retry.
            identity += ":route:" + ":".join(str(route_trigger.get(key) or "") for key in (
                "stop_domain", "reason", "event_at"))
        exit_trigger = snapshot.get("exit_trigger")
        if isinstance(exit_trigger, dict) and exit_trigger.get("domain") == "OPTION_PREMIUM":
            # A retained premium trigger can be created after a bar watermark
            # at the same timestamp. Include its immutable identity so the
            # lifecycle RPC does not mistake that protection for a replay.
            identity += ":exit:" + ":".join(str(exit_trigger.get(key) or "") for key in (
                "domain", "reason", "source", "event_at", "received_at", "price",
                "effective_stop", "stop_source", "stop_ordering_status",
                "atr_raise_ordering_status", "raised_stop"))
        for ambiguity_key in ("ambiguous_premium_quote", "ambiguous_premium_bar"):
            ambiguity = snapshot.get(ambiguity_key)
            if (isinstance(ambiguity, dict)
                    and ambiguity.get("status") == "T1_ORDER_UNKNOWN"
                    and ambiguity.get("ordering_reason") in {"COST_STOP_ONLY_BREACH", "ATR_STOP_ONLY_BREACH"}):
                # A cost-only T1 ambiguity can arrive after a non-breaching
                # bar already committed this event-time watermark. Give that
                # newly observed uncertainty its own retry-stable identity.
                label = ("seeded-atr-ambiguity" if ambiguity.get("ordering_reason") == "ATR_STOP_ONLY_BREACH"
                         else "cost-stop-ambiguity")
                identity += ":" + label + ":" + ambiguity_key + ":" + ":".join(
                    str(ambiguity.get(key) or "") for key in (
                        "event_at", "source_minute", "received_at", "source", "price",
                        "option_close", "original_option_sl", "entry_premium"))
                if label == "seeded-atr-ambiguity":
                    identity += ":" + str(ambiguity.get("seeded_option_sl"))
        resolution = snapshot.get("ambiguity_resolution")
        if resolution:
            # Keep the silent resolution distinct from the watermark snapshot
            # that recorded the ambiguity, while retaining a retry-stable ID.
            identity += ":resolution:" + ":".join(str(resolution.get(key) or "") for key in (
                "source_minute", "event_at", "outcome", "evidence_source"))
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
            "pnl_points": (accounting.get("spot_total_points") if reference_accounting
                           else self._weighted_option_index_pnl(trade, index_price)) if terminal else None,
            "update_type": update_type, "option_trailing": deepcopy(opt), "alert_snapshot": alert_snapshot,
        }
        if reference_accounting:
            payload["option_pnl_points"] = accounting.get("option_total_points") if terminal else None
        pending = self._option_pending.setdefault(trade["id"], [])
        if not any(item[0] == event_id for item in pending):
            pending.append((event_id, payload))
        if self._option_locks.setdefault(trade["id"], asyncio.Lock()).locked():
            # Recovery/outbox persistence may be waiting on a slow provider RPC.
            # The observed protection is already applied in memory; queue this
            # transition and let the active writer drain it without blocking the
            # market evaluation loop on the same per-trade lock.
            self._option_flush_followups.add(trade["id"])
            self._schedule_option_flush(trade)
            return
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
                            stop_fields = ("locked_option_sl", "protection_mode", "premium_stop_source",
                                           "stop_effective_at", "previous_locked_option_sl")
                            committed_rev = persisted_opt.get("revision")
                            if committed_rev and committed_rev != revision:
                                self._option_revisions[trade_id] = committed_rev
                                opt["revision"] = committed_rev
                                opt["alert_events"] = deepcopy(persisted_opt.get("alert_events") or {})
                                if persisted_opt.get("phase") == "CLOSED":
                                    opt.update(persisted_opt)
                                    trade["state"] = "CLOSED"
                                    pending.clear()
                                    self._cleanup_option_selection_if_terminal()
                                else:
                                    # Drop observations superseded by a newer committed milestone/stop.
                                    kept = []
                                    for queued_id, queued in pending:
                                        candidate = queued["option_trailing"]
                                        unresolved = next((key for key in (
                                            "ambiguous_index_touch", "ambiguous_premium_quote", "ambiguous_premium_bar")
                                            if (persisted_opt.get(key) or {}).get("status") in {
                                                "ORDER_UNKNOWN", "T1_ORDER_UNKNOWN", "T2_ORDER_UNKNOWN"}), None)
                                        if not unresolved and persisted_opt.get("deferred_option_exit"):
                                            unresolved = "deferred_option_exit"
                                        if persisted_opt.get("policy_version") == "option_premium_protection_v2" and unresolved:
                                            # A stale writer cannot certify an allocation or erase
                                            # another writer's unresolved protective observation.
                                            rebased = deepcopy(persisted_opt)
                                            ambiguity = rebased[unresolved]
                                            for key in ("subsequent_index_observations", "subsequent_premium_observations"):
                                                observations = ambiguity.setdefault(key, [])
                                                for context in (opt, candidate):
                                                    for item in (context.get(unresolved) or {}).get(key) or []:
                                                        if item not in observations:
                                                            observations.append(deepcopy(item))
                                            mark = candidate.get("last_index_mark")
                                            if mark:
                                                mark = {**mark, "high": mark["price"], "low": mark["price"], "candle_timestamp": None}
                                            if mark and mark not in ambiguity["subsequent_index_observations"]:
                                                ambiguity["subsequent_index_observations"].append(deepcopy(mark))
                                            trigger = candidate.get("exit_trigger")
                                            if trigger and trigger != rebased.get("exit_trigger"):
                                                observations = ambiguity["subsequent_premium_observations"]
                                                if trigger not in observations:
                                                    observations.append(deepcopy(trigger))
                                                if not rebased.get("exit_trigger"):
                                                    # Cost-only uncertainty may have no proven exit.
                                                    # A later local breach still routes protection,
                                                    # while its reference closure remains deferred.
                                                    rebased["exit_trigger"] = deepcopy(trigger)
                                                    for field in ("execution_route_pending", "execution_route_reason", "execution_route_trigger"):
                                                        rebased[field] = deepcopy(candidate.get(field))
                                                    protective = candidate.get("exit") or candidate.get("deferred_option_exit")
                                                    if protective:
                                                        ambiguity.setdefault("protective_option_exit", deepcopy(protective))
                                            observations = rebased.setdefault("pending_index_observations", [])
                                            for item in candidate.get("pending_index_observations") or []:
                                                if item not in observations:
                                                    observations.append(deepcopy(item))
                                            quote = candidate.get("last_option_quote")
                                            previous_quote = rebased.get("last_option_quote") or {}
                                            if quote and (not previous_quote.get("observed_at") or
                                                    parse_candle_timestamp(quote["observed_at"]) >= parse_candle_timestamp(previous_quote["observed_at"])):
                                                rebased["last_option_quote"] = deepcopy(quote)
                                            if (candidate.get("locked_option_sl") is not None and
                                                    candidate["locked_option_sl"] > (rebased.get("locked_option_sl") or 0)):
                                                rebased.update({field: deepcopy(candidate.get(field)) for field in stop_fields})
                                            for field in ("last_processed_option_bar_end", "last_valid_option_bar_end",
                                                          "last_index_event_at", "quote_watermark"):
                                                value = candidate.get(field)
                                                if value and (not rebased.get(field) or parse_candle_timestamp(value) > parse_candle_timestamp(rebased[field])):
                                                    rebased[field] = value
                                            queued.update(option_trailing=rebased, active_state=persisted.get("active_state"),
                                                          score=rebased.get("highest_milestone"), update_type=None,
                                                          alert_snapshot=None, exit_reason=None, exit_price=None,
                                                          exit_timestamp=None, pnl_points=None, option_pnl_points=None)
                                            candidate = rebased
                                            # Reconcile lifecycle/protection intent without rolling
                                            # back newer live market evidence or a queued stop raise.
                                            for field in ("phase", "highest_milestone", "booked_fraction", "remaining_fraction",
                                                          "t1_at", "t2_at", "index_guard_enabled", "index_guard_stop",
                                                          "ambiguous_index_touch", "ambiguous_premium_quote", "ambiguous_premium_bar",
                                                          "pending_index_observations", "observed_index_guard_exit", "deferred_option_exit",
                                                          "exit", "exit_trigger", "execution_route_pending", "execution_route_reason",
                                                          "execution_route_trigger", "execution_status", "accounting",
                                                          "monitoring_status", "monitoring_detail"):
                                                opt[field] = deepcopy(rebased.get(field))
                                            if (opt.get("locked_option_sl") is None or
                                                    (rebased.get("locked_option_sl") or 0) > opt["locked_option_sl"]):
                                                opt.update({field: deepcopy(rebased.get(field)) for field in stop_fields})
                                            trade["state"] = queued["active_state"]
                                        if candidate.get("accounting_formula_version") == "one_lot_reference_v1":
                                            candidate["accounting"] = self._merge_reference_evidence(
                                                persisted_opt.get("accounting"), candidate.get("accounting"))
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
                                            if (candidate.get("accounting_formula_version") == "one_lot_reference_v1"
                                                    and candidate.get("highest_milestone", 0) < 2
                                                    and persisted_opt.get("highest_milestone", 0) >= 2):
                                                t2_time = persisted_opt.get("t2_at")
                                                exit_time = queued.get("exit_timestamp")
                                                if (not t2_time or not exit_time or
                                                        parse_candle_timestamp(t2_time) > parse_candle_timestamp(exit_time)):
                                                    raise RuntimeError("Committed T2 may follow observed exit; reconciliation required")
                                            for field in ("highest_milestone", "booked_fraction"):
                                                candidate[field] = max(candidate.get(field) or 0, persisted_opt.get(field) or 0)
                                            candidate["exit"]["exited_fraction"] = round(1.0 - candidate["booked_fraction"], 10)
                                            committed_stop = persisted_opt.get("locked_option_sl")
                                            observed_stop = candidate.get("locked_option_sl")
                                            if committed_stop is not None and (observed_stop is None or committed_stop > observed_stop):
                                                candidate.update({field: deepcopy(persisted_opt.get(field)) for field in stop_fields})
                                            candidate.update(phase="CLOSED", remaining_fraction=0.0)
                                            queued["score"] = candidate["highest_milestone"]
                                            rebased_trade = deepcopy(trade)
                                            rebased_trade["market_context"]["option_trailing"] = candidate
                                            if candidate.get("accounting_formula_version") == "one_lot_reference_v1":
                                                rebased_accounting = self._reference_totals(rebased_trade)
                                                queued["pnl_points"] = rebased_accounting.get("spot_total_points")
                                                queued["option_pnl_points"] = rebased_accounting.get("option_total_points")
                                            else:
                                                queued["pnl_points"] = self._weighted_option_index_pnl(rebased_trade, queued.get("exit_price"))
                                            alert = queued.get("alert_snapshot")
                                            if alert:
                                                alert["option_trailing"] = deepcopy(candidate)
                                                alert["option_trailing"].pop("alert_events", None)
                                                alert["trade"]["market_context"]["option_trailing"] = deepcopy(alert["option_trailing"])
                                            opt.update({key: candidate[key] for key in
                                                        ("highest_milestone", "booked_fraction", "exit") + stop_fields
                                                        if key in candidate})
                                            kept.append((queued_id, queued))
                                        elif older_phase and candidate.get("locked_option_sl") is not None and (
                                                persisted_opt.get("locked_option_sl") is None or candidate["locked_option_sl"] > persisted_opt["locked_option_sl"]):
                                            # A later database milestone must not erase a newer observed raise.
                                            for field in ("phase", "highest_milestone", "booked_fraction", "remaining_fraction",
                                                          "index_guard_enabled", "index_guard_stop", "t1_at", "t2_at",
                                                          "observed_index_guard_exit", "pending_index_observations",
                                                          "deferred_option_exit"):
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
                                                opt.update({key: candidate[key] for key in ("phase", "highest_milestone", "booked_fraction", "remaining_fraction", "index_guard_enabled", "index_guard_stop", "locked_option_sl", "protection_mode", "observed_index_guard_exit", "pending_index_observations", "deferred_option_exit") if key in candidate})
                                                trade["state"] = queued["active_state"]
                                            kept.append((queued_id, queued))
                                        elif older_stop and (candidate.get("highest_milestone", 0) > persisted_opt.get("highest_milestone", 0)
                                                or (not older_phase and (candidate.get("pending_index_observations") or candidate.get("observed_index_guard_exit")))):
                                            # Milestone progress and stop strength are independent:
                                            # retain new touches/backfill guards with committed protection.
                                            candidate.update({field: deepcopy(persisted_opt.get(field)) for field in stop_fields})
                                            alert = queued.get("alert_snapshot")
                                            if alert:
                                                alert["option_trailing"] = deepcopy(candidate)
                                                alert["option_trailing"].pop("alert_events", None)
                                                alert["trade"]["market_context"]["option_trailing"] = deepcopy(alert["option_trailing"])
                                            # Later queued bars may already have raised live protection.
                                            if opt.get("locked_option_sl") is None or candidate["locked_option_sl"] > opt["locked_option_sl"]:
                                                opt.update({key: candidate[key] for key in stop_fields})
                                            kept.append((queued_id, queued))
                                        elif not older_phase and not older_stop:
                                            kept.append((queued_id, queued))
                                    for _, kept_payload in kept:
                                        candidate = kept_payload["option_trailing"]
                                        if candidate.get("accounting_formula_version") == "one_lot_reference_v1":
                                            rebased_trade = deepcopy(trade)
                                            rebased_trade["market_context"]["option_trailing"] = candidate
                                            accounting = self._reference_totals(rebased_trade)
                                            if kept_payload.get("active_state") in {"CLOSED", "STOPPED_OUT"}:
                                                kept_payload["pnl_points"] = accounting.get("spot_total_points")
                                                kept_payload["option_pnl_points"] = accounting.get("option_total_points")
                                            opt["accounting"] = self._merge_reference_evidence(accounting, opt.get("accounting"))
                                            self._reference_totals(trade)
                                            alert = kept_payload.get("alert_snapshot")
                                            if alert:
                                                alert["option_trailing"] = deepcopy(candidate)
                                                alert["option_trailing"].pop("alert_events", None)
                                                alert["trade"]["market_context"]["option_trailing"] = deepcopy(alert["option_trailing"])
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
                                        for field in ("last_processed_option_bar_end", "last_valid_option_bar_end", "last_index_event_at", "quote_watermark"):
                                            committed_at = persisted_opt.get(field)
                                            if committed_at and (not candidate.get(field) or parse_candle_timestamp(candidate[field]) < parse_candle_timestamp(committed_at)):
                                                candidate[field] = committed_at
                                        quote_watermark = candidate.get("quote_watermark")
                                        if quote_watermark and (not opt.get("quote_watermark") or parse_candle_timestamp(opt["quote_watermark"]) < parse_candle_timestamp(quote_watermark)):
                                            opt["quote_watermark"] = quote_watermark
                                        alert = kept_payload.get("alert_snapshot")
                                        if alert:
                                            alert["option_trailing"] = deepcopy(candidate)
                                            alert["option_trailing"].pop("alert_events", None)
                                            alert["trade"]["market_context"]["option_trailing"] = deepcopy(alert["option_trailing"])
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
                if opt.get("phase") == "CLOSED" or trade.get("state") in {"CLOSED", "STOPPED_OUT"}:
                    self._cleanup_option_selection_if_terminal()
                if opt.get("accounting_formula_version") == "one_lot_reference_v1":
                    try:
                        # The RPC may return evidence merged by another writer,
                        # including on idempotent replay. Add it to later queued
                        # transitions without replacing their newer phase or stop.
                        committed_accounting = committed_opt.get("accounting")
                        live_accounting = self._merge_reference_evidence(committed_accounting, opt.get("accounting"))
                        staged = []
                        for _, later in pending:
                            candidate = deepcopy(later["option_trailing"])
                            candidate["accounting"] = self._merge_reference_evidence(
                                committed_accounting, candidate.get("accounting"))
                            staged_trade = deepcopy(trade)
                            staged_trade["market_context"]["option_trailing"] = candidate
                            totals = self._reference_totals(staged_trade)
                            staged.append((later, candidate, totals))
                        opt["accounting"] = live_accounting
                        self._reference_totals(trade)
                        for later, candidate, totals in staged:
                            later["option_trailing"] = candidate
                            if later.get("active_state") in {"CLOSED", "STOPPED_OUT"}:
                                later["pnl_points"] = totals.get("spot_total_points")
                                later["option_pnl_points"] = totals.get("option_total_points")
                            alert = later.get("alert_snapshot")
                            if alert:
                                alert["option_trailing"] = deepcopy(candidate)
                                alert["option_trailing"].pop("alert_events", None)
                                alert["trade"]["market_context"]["option_trailing"] = deepcopy(alert["option_trailing"])
                    except (TypeError, ValueError, RuntimeError):
                        logger.exception("Committed reference evidence conflicts with newer observation for %s", trade_id)
                        return
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

    def _schedule_option_flush(self, trade):
        """Start one non-blocking retry task for a trade's lifecycle outbox."""
        self._option_runtime()
        trade_id = trade["id"]
        tasks = self._option_flush_tasks
        existing = tasks.get(trade_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(self._flush_option_trade(trade))
        tasks[trade_id] = task

        def clear(done):
            if tasks.get(trade_id) is done:
                tasks.pop(trade_id, None)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    logger.warning("Background option lifecycle retry failed for %s: %s", trade_id, error)
                followups = self._option_flush_followups
                if trade_id in followups:
                    followups.discard(trade_id)
                    if self._option_pending.get(trade_id):
                        self._schedule_option_flush(trade)

        task.add_done_callback(clear)
        return task

    async def retry_option_lifecycle_writes(self, *, defer_for_protection=False):
        """Retry lifecycle writes, optionally keeping protection independent of storage latency."""
        self._option_runtime()
        for trade in list(getattr(self, "active_trades", [])) + list(getattr(self, "_option_alert_trades", [])):
            if _has_option_policy((trade.get("market_context") or {}).get("option_trailing") or {}):
                if defer_for_protection:
                    alerts = ((trade.get("market_context") or {}).get("option_trailing") or {}).get("alert_events") or {}
                    has_retryable_alert = any(event.get("status") in {"PENDING", "FAILED"}
                                              for event in alerts.values())
                    if self._option_pending.get(trade["id"]) or has_retryable_alert:
                        self._schedule_option_flush(trade)
                else:
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
        v2 = opt.get("policy_version") == "option_premium_protection_v2"
        if v2 and not opt.get("atr_enabled"):
            return
        previous_stop = float(opt.get("locked_option_sl") or opt["original_option_sl"])
        stop, mode, raised = ratchet_option_stop(
            previous_stop, bar.raw_line, opt["original_option_sl"],
            protection_mode=opt.get("protection_mode", "NONE"),
            entry_premium_floor=opt.get("original_option_entry") if v2 else None,
        )
        opt["raw_line"] = bar.raw_line
        opt["last_valid_option_bar_end"] = bar.end_time
        if raised:
            opt.update(previous_locked_option_sl=previous_stop, locked_option_sl=stop,
                       protection_mode=mode, atr_eligible=mode == "ATR",
                       premium_stop_source="ATR_TRAILING" if v2 and mode == "ATR" else opt.get("premium_stop_source"),
                       stop_effective_at=event_at.isoformat())

    def _prepare_option_history(self, histories, observed_at):
        self._option_runtime()
        result = {}
        for trade in self.active_trades:
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            if not _has_option_policy(opt):
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
            cached = self._option_history.get(trade["id"], [])
            seed_start = opt.get("indicator_seed_start") or (cached[0].start_time if cached else None)
            if seed_start:
                opt["indicator_seed_start"] = seed_start
                seed_at = parse_candle_timestamp(seed_start)
                # Wilder smoothing depends on the original warmup prefix, even
                # when a shortened response still includes the durable watermark.
                normalized = [bar for bar in normalized if bar.start_time >= seed_at]
                if not normalized or normalized[0].start_time != seed_at:
                    result[trade["id"]] = []
                    continue
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
                opt["indicator_seed_start"] = bars[0].start_time
                self._option_history[trade["id"]] = bars
        return result

    @staticmethod
    def _retained_t1_premium_exit(opt):
        for key in ("ambiguous_premium_bar", "ambiguous_premium_quote"):
            protective = ((opt.get(key) or {}).get("protective_option_exit") or {})
            if protective:
                return protective
        return None

    @staticmethod
    def _unresolved_t1_premium_ambiguity(opt):
        for key in ("ambiguous_premium_bar", "ambiguous_premium_quote"):
            ambiguity = opt.get(key) or {}
            if ambiguity.get("status") != "T1_ORDER_UNKNOWN":
                continue
            if ambiguity.get("protective_option_exit"):
                return ambiguity
            if ambiguity.get("ordering_reason") in {"COST_STOP_ONLY_BREACH", "ATR_STOP_ONLY_BREACH"}:
                return ambiguity
            try:
                price = float(ambiguity["price"])
                original_stop = float(opt["original_option_sl"])
                entry_premium = float(opt["original_option_entry"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if original_stop < price <= entry_premium:
                return ambiguity
        return None

    async def _retain_t1_premium_bar_exit(self, trade, bar):
        """Keep a stop breach tied to T1 protective without choosing its allocation."""
        opt = trade["market_context"]["option_trailing"]
        if self._retained_t1_premium_exit(opt):
            return None
        try:
            price = float(bar.close)
            stop = float(opt["original_option_sl"])
            event_at = parse_candle_timestamp(bar.end_time)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
            return None
        if not isfinite(price) or price <= 0 or price > stop:
            return None
        source = "completed_5m_option_close"
        protective = {
            "reason": "SL_HIT", "stop_domain": "OPTION_PREMIUM",
            "source": source, "trigger_source": source,
            "event_at": event_at.isoformat(), "option_close": price,
            "trigger_price": price, "option_stop": stop,
            "effective_stop": stop, "stop_source": "INITIAL_OPTION_SL",
        }
        opt["ambiguous_premium_bar"] = {
            "status": "T1_ORDER_UNKNOWN", "event_at": event_at.isoformat(),
            "source_minute": parse_candle_timestamp(bar.start_time).isoformat(),
            "option_close": price, "protective_option_exit": protective,
        }
        opt["source_candle"] = {"start_time": bar.start_time, "end_time": bar.end_time,
                                "close": price}
        opt["last_valid_option_bar_end"] = bar.end_time
        opt["last_processed_option_bar_end"] = bar.end_time
        opt["exit_trigger"] = {
            "domain": "OPTION_PREMIUM", "source": source,
            "event_at": event_at.isoformat(), "price": price,
            "effective_stop": stop, "stop_source": "INITIAL_OPTION_SL", "reason": "SL_HIT",
        }
        opt["execution_status"] = (
            "REFERENCE_TRIGGERED_NO_BROKER_FILL"
            if opt.get("execution_status") in {"REFERENCE_ONLY", "REFERENCE_TRIGGERED_NO_BROKER_FILL"}
            else "EXIT_TRIGGERED_PENDING_CONFIRMATION"
        )
        self._mark_option_execution_route_pending(trade, "SL_HIT")
        opt.setdefault("event_history", []).append(dict(opt["exit_trigger"]))
        await self._record_trade_update(trade, None, None, event_at.isoformat())
        return "SL_HIT" if opt.get("execution_route_pending") else None

    async def _retain_t1_premium_bar_ambiguity(self, trade, bar, index_observations=None):
        """Keep a T1-tied close from applying a newly activated cost or seeded ATR stop."""
        opt = trade["market_context"]["option_trailing"]
        try:
            price = float(bar.close)
            original_stop = float(opt["original_option_sl"])
            entry_premium = float(opt["original_option_entry"])
            event_at = parse_candle_timestamp(bar.end_time)
            seeded_stop = None
            if (opt.get("policy_version") == "option_premium_protection_v2"
                    and opt.get("premium_stop_source") == "ATR_TRAILING"
                    and opt.get("t1_at") and opt.get("stop_effective_at")
                    and event_at == parse_candle_timestamp(opt["t1_at"])
                    and event_at == parse_candle_timestamp(opt["stop_effective_at"])):
                seeded_stop = float(opt["locked_option_sl"])
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
            return False
        ceiling = max(entry_premium, seeded_stop) if seeded_stop is not None and isfinite(seeded_stop) else entry_premium
        if (not isfinite(price) or price <= original_stop or price > ceiling
                or self._retained_t1_premium_exit(opt)):
            return False
        opt["ambiguous_premium_bar"] = {
            "status": "T1_ORDER_UNKNOWN", "event_at": event_at.isoformat(),
            "source_minute": parse_candle_timestamp(bar.start_time).isoformat(),
            "option_close": price,
            "ordering_reason": "ATR_STOP_ONLY_BREACH" if price > entry_premium else "COST_STOP_ONLY_BREACH",
            "original_option_sl": original_stop,
            "entry_premium": entry_premium,
            "tied_index_observations": list(index_observations or []),
        }
        if price > entry_premium:
            opt["ambiguous_premium_bar"]["seeded_option_sl"] = seeded_stop
        opt["source_candle"] = {"start_time": bar.start_time, "end_time": bar.end_time,
                                "close": price}
        opt["last_valid_option_bar_end"] = bar.end_time
        opt["last_processed_option_bar_end"] = bar.end_time
        await self._record_trade_update(trade, None, None, event_at.isoformat())
        return True

    async def _retain_t1_premium_quote_exit(self, trade, quote, event_at, index_observations):
        """Retain an exact-T1 quote breach before index milestones are committed."""
        opt = trade["market_context"]["option_trailing"]
        if self._retained_t1_premium_exit(opt):
            return None
        try:
            observed_at = parse_candle_timestamp(quote["observed_at"])
            received_at = parse_candle_timestamp(quote["received_at"])
            price = float(quote["price"])
            stop = float(opt["original_option_sl"])
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return None
        if (observed_at != event_at or received_at < observed_at
                or received_at - observed_at > timedelta(seconds=5)
                or not isfinite(price) or price <= 0 or price > stop):
            return None
        source = quote.get("source") or "OPTION_QUOTE"
        protective = {
            "reason": "SL_HIT", "stop_domain": "OPTION_PREMIUM",
            "source": source, "trigger_source": source,
            "event_at": observed_at.isoformat(), "received_at": received_at.isoformat(),
            "option_close": price, "trigger_price": price,
            "option_stop": stop, "effective_stop": stop,
            "stop_source": "INITIAL_OPTION_SL",
        }
        opt["ambiguous_premium_quote"] = {
            "event_at": observed_at.isoformat(), "price": price,
            "status": "T1_ORDER_UNKNOWN", "received_at": received_at.isoformat(),
            "protective_option_exit": protective,
            "tied_index_observations": index_observations,
        }
        opt["quote_watermark"] = observed_at.isoformat()
        opt["last_option_quote"] = {
            "price": price, "observed_at": observed_at.isoformat(),
            "received_at": received_at.isoformat(), "source": source,
        }
        opt["exit_trigger"] = {
            "domain": "OPTION_PREMIUM", "source": source,
            "event_at": observed_at.isoformat(), "received_at": received_at.isoformat(),
            "price": price, "effective_stop": stop,
            "stop_source": "INITIAL_OPTION_SL", "reason": "SL_HIT",
        }
        opt["execution_status"] = (
            "REFERENCE_TRIGGERED_NO_BROKER_FILL"
            if opt.get("execution_status") in {"REFERENCE_ONLY", "REFERENCE_TRIGGERED_NO_BROKER_FILL"}
            else "EXIT_TRIGGERED_PENDING_CONFIRMATION"
        )
        self._mark_option_execution_route_pending(trade, "SL_HIT")
        opt.setdefault("event_history", []).append(dict(opt["exit_trigger"]))
        await self._record_trade_update(trade, None, None, observed_at.isoformat())
        return "SL_HIT" if opt.get("execution_route_pending") else None

    async def _retain_t1_premium_quote_ambiguity(self, trade, quote, event_at, index_observations,
                                                *, committed_t2_at=None, previously_accepted=False):
        """Retain a T1-tied quote that breaches cost only if T1 came first."""
        opt = trade["market_context"]["option_trailing"]
        try:
            observed_at = parse_candle_timestamp(quote["observed_at"])
            received_at = parse_candle_timestamp(quote["received_at"])
            price = float(quote["price"])
            original_stop = float(opt["original_option_sl"])
            entry_premium = float(opt["original_option_entry"])
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return False
        if (observed_at != event_at or received_at < observed_at
                or received_at - observed_at > timedelta(seconds=5)
                or not isfinite(price) or not original_stop < price <= entry_premium
                or self._unresolved_t1_premium_ambiguity(opt)):
            return False
        watermark = opt.get("quote_watermark")
        if (committed_t2_at is None and not previously_accepted
                and self._option_quote_is_replay(opt, observed_at, price)):
            return False
        source = quote.get("source") or "OPTION_QUOTE"
        opt["ambiguous_premium_quote"] = {
            "event_at": observed_at.isoformat(), "price": price,
            "status": "T1_ORDER_UNKNOWN", "received_at": received_at.isoformat(),
            "source": source,
            "ordering_reason": "COST_STOP_ONLY_BREACH",
            "original_option_sl": original_stop, "entry_premium": entry_premium,
            "tied_index_observations": list(index_observations),
        }
        if committed_t2_at is not None:
            opt["ambiguous_premium_quote"].update(
                tied_milestone={"kind": "T2_HIT", "event_at": committed_t2_at.isoformat(),
                                "already_committed": True},
                allocation_uncertainty={"pre_t1_no_exit": True,
                                        "post_t1_pre_t2_exit_fraction": 0.5,
                                        "post_t2_runner_fraction": float(opt.get("remaining_fraction", 0.2))})
        if not watermark or observed_at > parse_candle_timestamp(watermark):
            opt["quote_watermark"] = observed_at.isoformat()
        if not previously_accepted:
            opt["last_option_quote"] = {
                "price": price, "observed_at": observed_at.isoformat(),
                "received_at": received_at.isoformat(), "source": source,
            }
        await self._record_trade_update(trade, None, None, observed_at.isoformat())
        return True

    async def _close_option_premium_trade(self, trade, price, event_at, *, source, received_at=None,
                                          phase_override=None, stop_override=None,
                                          stop_source_override=None, stop_ordering_status=None,
                                          raised_stop=None):
        """Record a premium-domain trigger without manufacturing a spot mark or fill."""
        opt = trade["market_context"]["option_trailing"]
        if trade.get("state") in {"CLOSED", "STOPPED_OUT"}:
            return None
        if self._retained_t1_premium_exit(opt):
            # The first stop breach is already routed, while the T1 allocation
            # remains order-unknown. Later quotes/bars cannot finalize or
            # rewrite that reference exit until its ordering is resolved.
            return None
        phase = phase_override or opt.get("phase")
        stop = float(stop_override if stop_override is not None else
                     opt.get("locked_option_sl") or opt["original_option_sl"])
        stop_source = stop_source_override or (
            ("INITIAL_OPTION_SL" if phase == "PRE_T1" else opt.get("premium_stop_source")) or
            ("INITIAL_OPTION_SL" if phase == "PRE_T1" else "BREAK_EVEN_COST"))
        if phase == "PRE_T1":
            reason = "SL_HIT"
        elif stop_source == "ATR_TRAILING":
            reason = "ATR_SL_HIT"
        elif stop_source == "BREAK_EVEN_COST":
            reason = "STOPPED_OUT_AT_BE"
        else:
            reason = "SL_HIT"
        remainder = float(opt.get("remaining_fraction", 1.0))
        observed_at = parse_candle_timestamp(event_at).isoformat()
        trigger = {
            "domain": "OPTION_PREMIUM", "source": source,
            "event_at": observed_at, "received_at": received_at,
            "price": float(price), "effective_stop": stop,
            "stop_source": stop_source, "reason": reason,
        }
        if stop_ordering_status:
            trigger["stop_ordering_status"] = stop_ordering_status
        if raised_stop is not None:
            trigger["raised_stop"] = float(raised_stop)
        t1_premium_ambiguity = self._unresolved_t1_premium_ambiguity(opt)
        ambiguity = opt.get("ambiguous_index_touch") or t1_premium_ambiguity
        queued_index_before_or_at_exit = False
        for observation in opt.get("pending_index_observations") or []:
            try:
                queued_at = parse_candle_timestamp(observation["event_at"])
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                queued_index_before_or_at_exit = True
                break
            if queued_at <= parse_candle_timestamp(observed_at):
                queued_index_before_or_at_exit = True
                break
        if ambiguity or queued_index_before_or_at_exit:
            pending_exit = {
                "reason": reason, "stop_domain": "OPTION_PREMIUM", "source": source,
                "event_at": observed_at, "received_at": received_at,
                "option_close": float(price), "option_stop": stop, "effective_stop": stop,
                "stop_source": stop_source, "trigger_source": source,
                "trigger_price": float(price), "detected_at": datetime.now(timezone.utc).isoformat(),
            }
            if stop_ordering_status:
                pending_exit["stop_ordering_status"] = stop_ordering_status
            if raised_stop is not None:
                pending_exit["raised_stop"] = float(raised_stop)
            # The first observed stop trigger is immutable; later quotes cannot
            # move its event time or price while index ordering/history is unresolved.
            pending_container = ambiguity if ambiguity else opt
            pending_key = "protective_option_exit" if ambiguity else "deferred_option_exit"
            existing_pending = pending_container.get(pending_key)
            already_pending = existing_pending is not None
            if already_pending:
                pending_exit = existing_pending
            else:
                pending_container[pending_key] = pending_exit
            for key, value in {
                "stop_domain": "OPTION_PREMIUM", "source": source,
                "received_at": received_at, "option_close": float(price),
                "option_stop": stop, "effective_stop": stop,
                "stop_source": stop_source, "trigger_source": source,
                "trigger_price": float(price),
                "stop_ordering_status": stop_ordering_status,
                "raised_stop": float(raised_stop) if raised_stop is not None else None,
            }.items():
                if value is not None:
                    pending_exit.setdefault(key, value)
            opt["exit_trigger"] = {
                "domain": "OPTION_PREMIUM", "source": pending_exit.get("source"),
                "event_at": pending_exit["event_at"], "received_at": pending_exit.get("received_at"),
                "price": pending_exit["option_close"],
                "effective_stop": pending_exit.get("effective_stop", pending_exit.get("option_stop")),
                "stop_source": pending_exit.get("stop_source"), "reason": pending_exit["reason"],
            }
            for key in ("stop_ordering_status", "raised_stop"):
                if pending_exit.get(key) is not None:
                    opt["exit_trigger"][key] = pending_exit[key]
            opt["execution_status"] = ("REFERENCE_TRIGGERED_NO_BROKER_FILL"
                                        if opt.get("execution_status") in {"REFERENCE_ONLY", "REFERENCE_TRIGGERED_NO_BROKER_FILL"}
                                        else "EXIT_TRIGGERED_PENDING_CONFIRMATION")
            self._mark_option_execution_route_pending(trade, pending_exit["reason"])
            if not already_pending:
                opt.setdefault("event_history", []).append(dict(opt["exit_trigger"]))
            await self._record_trade_update(trade, None, None, pending_exit["event_at"])
            return pending_exit["reason"]
        opt["exit_trigger"] = trigger
        opt["execution_status"] = ("REFERENCE_TRIGGERED_NO_BROKER_FILL"
                                    if opt.get("execution_status") in {"REFERENCE_ONLY", "REFERENCE_TRIGGERED_NO_BROKER_FILL"}
                                    else "EXIT_TRIGGERED_PENDING_CONFIRMATION")
        self._mark_option_execution_route_pending(trade, reason)
        runner_points = round(float(price) - float(opt["original_option_entry"]), 8)
        opt["exit"] = {
            "reason": reason, "stop_domain": "OPTION_PREMIUM", "event_at": observed_at,
            "detected_at": datetime.now(timezone.utc).isoformat(), "received_at": received_at,
            "option_close": float(price), "reference_exit_price": float(price),
            "option_stop": stop, "stop_source": stop_source,
            "trigger_source": source, "trigger_price": float(price),
            "exited_fraction": remainder,
            "runner_option_points": 0.0 if reason == "STOPPED_OUT_AT_BE" else runner_points,
            "fill_price": None,
        }
        for key in ("stop_ordering_status", "raised_stop"):
            if trigger.get(key) is not None:
                opt["exit"][key] = trigger[key]
        opt.update(phase="CLOSED", remaining_fraction=0.0, index_guard_enabled=False,
                   monitoring_status="CLOSED")
        trade["state"] = "CLOSED"
        exit_index_price = None
        if opt.get("accounting_formula_version") == "one_lot_reference_v1":
            self._capture_reference_leg(trade, "EXIT", None, observed_at, option_close=float(price))
            exit_leg = opt["accounting"]["legs"]["EXIT"]
            exit_leg.update(option_source=source, option_observed_at=observed_at,
                            option_approximate=False, trigger_price=float(price),
                            effective_stop=stop, stop_source=stop_source)
            exit_index_price = exit_leg.get("spot_price")
            opt["exit"]["index_price"] = exit_index_price
            opt["exit"]["runner_option_points"] = (
                0.0 if reason == "STOPPED_OUT_AT_BE" else runner_points)
        opt.setdefault("event_history", []).append(trigger)
        await self._record_trade_update(trade, exit_index_price, reason, observed_at)
        return reason

    @staticmethod
    def _atr_raise_quote_stop_basis(opt, observed_at, price):
        """Return the prior stop only when a raise-tied quote breached it too."""
        if (opt.get("policy_version") != "option_premium_protection_v2"
                or opt.get("premium_stop_source") != "ATR_TRAILING"
                or not opt.get("stop_effective_at")):
            return None
        try:
            if parse_candle_timestamp(opt["stop_effective_at"]) != parse_candle_timestamp(observed_at):
                return None
            previous = float(opt["previous_locked_option_sl"])
            raised = float(opt["locked_option_sl"])
            quote_price = float(price)
            original = float(opt["original_option_sl"])
            entry = float(opt["original_option_entry"])
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return None
        if (not all(isfinite(value) for value in (previous, raised, quote_price, original, entry))
                or previous <= 0 or raised <= previous or quote_price <= 0 or quote_price > previous):
            return None
        previous_source = ("INITIAL_OPTION_SL" if previous <= original else
                           "BREAK_EVEN_COST" if previous <= entry else "ATR_TRAILING")
        return {"previous_stop": previous, "previous_stop_source": previous_source,
                "raised_stop": raised}

    @staticmethod
    def _option_quote_is_replay(opt, observed_at, price):
        """A source second can contain new prices; only identical observations are duplicates."""
        watermark = opt.get("quote_watermark")
        if not watermark:
            return False
        try:
            watermark_at = parse_candle_timestamp(watermark)
        except (TypeError, ValueError, OverflowError, OSError):
            return True
        if observed_at != watermark_at:
            return observed_at < watermark_at
        previous = opt.get("last_option_quote") or {}
        if not isinstance(previous, dict):
            return False
        try:
            return (previous.get("observed_at") is not None
                    and parse_candle_timestamp(previous["observed_at"]) == observed_at
                    and float(previous["price"]) == float(price))
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return False

    async def _reject_quote_before_active_stop(self, trade, observed_at, received_at, *, price=None,
                                               source=None, events=None):
        opt = (trade.get("market_context") or {}).get("option_trailing") or {}
        t2_value = opt.get("t2_at")
        if (opt.get("phase") == "POST_T2" and t2_value
                and observed_at <= parse_candle_timestamp(t2_value)):
            if await self._retain_t2_tied_quote_if_protective(
                    trade, observed_at, received_at, price, source, events):
                return True
            detail = "Rejected out-of-order option quote at or before T2 transition"
        else:
            effective_value = opt.get("stop_effective_at")
            if not effective_value:
                return False
            effective_at = parse_candle_timestamp(effective_value)
            tied_atr_raise = (opt.get("policy_version") == "option_premium_protection_v2"
                              and opt.get("premium_stop_source") == "ATR_TRAILING"
                              and observed_at == effective_at)
            if observed_at >= effective_at:
                if not tied_atr_raise:
                    return False
                if self._atr_raise_quote_stop_basis(opt, observed_at, price) is not None:
                    return False
            detail = ("Rejected option quote tied to ATR stop raise; event order unavailable"
                      if tied_atr_raise else
                      "Rejected out-of-order option quote before active stop effective time")
        if opt.get("policy_version") == "option_premium_protection_v2":
            before = (opt.get("monitoring_status"), list(opt.get("monitoring_unavailable_sources") or []),
                      dict(opt.get("monitoring_unavailable_details") or {}))
            previous, current = self._set_option_monitoring_health(
                opt, "OPTION_QUOTE", unavailable=True, detail=detail)
            changed = before != (current, opt.get("monitoring_unavailable_sources"),
                                 opt.get("monitoring_unavailable_details"))
            if changed:
                update = "MONITORING_UNAVAILABLE" if previous != "UNAVAILABLE" and current == "UNAVAILABLE" else None
                await self._record_trade_update(trade, None, update, received_at.isoformat())
        elif opt.get("monitoring_status") != "UNAVAILABLE" or opt.get("monitoring_detail") != detail:
            opt["monitoring_status"] = "UNAVAILABLE"
            opt["monitoring_detail"] = detail
            await self._record_trade_update(trade, None, "MONITORING_UNAVAILABLE", received_at.isoformat())
        return True

    async def _retain_t2_tied_quote_if_protective(self, trade, observed_at, received_at,
                                                 price, source, events):
        """Capture exact-T2 protection or cost uncertainty before watermark rejection."""
        opt = (trade.get("market_context") or {}).get("option_trailing") or {}
        t2_value = opt.get("t2_at")
        if (opt.get("phase") != "POST_T2" or not t2_value
                or observed_at != parse_candle_timestamp(t2_value)):
            return False
        retained, reason = await self._retain_t2_tied_premium_quote_exit(
            trade, observed_at, received_at, price, source)
        if retained and reason and events is not None:
            events.append((trade["id"], reason))
        return retained

    async def _retain_t2_tied_premium_quote_exit(self, trade, observed_at, received_at, price, source):
        """Retain a certain T2-tied breach or the uncertainty of a cost-only T1 tie."""
        opt = (trade.get("market_context") or {}).get("option_trailing") or {}
        if (opt.get("policy_version") != "option_premium_protection_v2"
                or opt.get("phase") != "POST_T2" or trade.get("state") != "T2_REACHED"):
            return False, None
        try:
            t2_at = parse_candle_timestamp(opt["t2_at"])
            quote_at = parse_candle_timestamp(observed_at)
            t1_at = parse_candle_timestamp(opt["t1_at"]) if opt.get("t1_at") else None
            received = parse_candle_timestamp(received_at)
            trigger_price = float(price)
            original_stop = float(opt["original_option_sl"])
            entry_premium = float(opt.get("original_option_entry") or original_stop)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return False, None
        if (quote_at != t2_at or received < quote_at or not isfinite(trigger_price)
                or trigger_price <= 0 or not isfinite(original_stop)):
            return False, None

        if t1_at is not None and t1_at > t2_at:
            return False, None
        t1_tied_to_t2 = t1_at is None or t1_at == t2_at
        atr_raise_ordering = None
        if t1_tied_to_t2:
            if trigger_price > original_stop:
                if trigger_price > entry_premium:
                    return False, None
                if self._retained_t1_premium_exit(opt):
                    return True, None
                retained = await self._retain_t1_premium_quote_ambiguity(
                    trade, {"observed_at": quote_at.isoformat(), "received_at": received.isoformat(),
                            "price": trigger_price, "source": source}, quote_at, [],
                    committed_t2_at=t2_at)
                return retained or bool(self._unresolved_t1_premium_ambiguity(opt)), None
            active_stop = original_stop
            stop_source = "INITIAL_OPTION_SL"
            reason = "SL_HIT"
        else:
            try:
                active_stop = float(opt.get("locked_option_sl") or original_stop)
            except (TypeError, ValueError, OverflowError):
                return False, None
            if not isfinite(active_stop) or trigger_price > active_stop:
                return False, None
            stop_source = opt.get("premium_stop_source") or (
                "INITIAL_OPTION_SL" if active_stop <= original_stop else
                "BREAK_EVEN_COST" if active_stop <= entry_premium else
                "ATR_TRAILING")
            stop_effective_at = opt.get("stop_effective_at")
            try:
                effective_at = parse_candle_timestamp(stop_effective_at) if stop_effective_at else None
                if effective_at is not None and quote_at < effective_at:
                    return False, None
                tied_atr_raise = stop_source == "ATR_TRAILING" and effective_at == quote_at
            except (TypeError, ValueError, OverflowError, OSError):
                return False, None
            if tied_atr_raise:
                prior_stop = self._atr_raise_quote_stop_basis(opt, quote_at, trigger_price)
                if prior_stop is None:
                    return False, None
                active_stop = prior_stop["previous_stop"]
                stop_source = prior_stop["previous_stop_source"]
                atr_raise_ordering = prior_stop
            reason = ("STOPPED_OUT_AT_BE" if stop_source == "BREAK_EVEN_COST" else
                      "ATR_SL_HIT" if stop_source == "ATR_TRAILING" else "SL_HIT")

        existing = opt.get("ambiguous_premium_quote")
        existing = existing if isinstance(existing, dict) else {}
        if existing.get("protective_option_exit"):
            return True, None
        prior_ambiguity = deepcopy(existing) if existing else None
        source = source or "OPTION_QUOTE"
        protective = {
            "reason": reason, "stop_domain": "OPTION_PREMIUM",
            "source": source, "trigger_source": source,
            "event_at": quote_at.isoformat(), "received_at": received.isoformat(),
            "option_close": trigger_price, "trigger_price": trigger_price,
            "option_stop": active_stop, "effective_stop": active_stop,
            "stop_source": stop_source,
            "stop_ordering_status": "T2_MILESTONE_TIED",
        }
        if reason == "STOPPED_OUT_AT_BE":
            protective["runner_option_points"] = 0.0
        if atr_raise_ordering is not None:
            protective["atr_raise_ordering_status"] = "ATR_RAISE_TIED"
            protective["raised_stop"] = atr_raise_ordering["raised_stop"]
        ambiguity = opt["ambiguous_premium_quote"] = {
            "status": "T2_ORDER_UNKNOWN", "event_at": quote_at.isoformat(),
            "price": trigger_price, "received_at": received.isoformat(), "source": source,
            "tied_milestone": {
                "kind": "T2_HIT", "event_at": t2_at.isoformat(),
                "booked_fraction": float(opt.get("booked_fraction", 0.8)),
                "remaining_fraction": float(opt.get("remaining_fraction", 0.2)),
            },
            "protective_option_exit": protective,
        }
        if prior_ambiguity:
            ambiguity["prior_ambiguity"] = prior_ambiguity
        possible_allocations = {}
        if t1_tied_to_t2:
            possible_allocations["pre_t1_exit_fraction"] = 1.0
        possible_allocations["post_t1_pre_t2_exit_fraction"] = 0.5
        possible_allocations["post_t2_runner_fraction"] = float(opt.get("remaining_fraction", 0.2))
        ambiguity["allocation_uncertainty"] = possible_allocations
        watermark = opt.get("quote_watermark")
        if not watermark or quote_at > parse_candle_timestamp(watermark):
            opt["quote_watermark"] = quote_at.isoformat()
        opt["last_option_quote"] = {
            "price": trigger_price, "observed_at": quote_at.isoformat(),
            "received_at": received.isoformat(), "source": source,
        }
        opt["exit_trigger"] = {
            "domain": "OPTION_PREMIUM", "source": source,
            "event_at": quote_at.isoformat(), "received_at": received.isoformat(),
            "price": trigger_price, "effective_stop": active_stop,
            "stop_source": stop_source, "reason": reason,
            "stop_ordering_status": "T2_MILESTONE_TIED",
        }
        if atr_raise_ordering is not None:
            opt["exit_trigger"]["atr_raise_ordering_status"] = "ATR_RAISE_TIED"
            opt["exit_trigger"]["raised_stop"] = atr_raise_ordering["raised_stop"]
        opt["execution_status"] = (
            "REFERENCE_TRIGGERED_NO_BROKER_FILL"
            if opt.get("execution_status") in {"REFERENCE_ONLY", "REFERENCE_TRIGGERED_NO_BROKER_FILL"}
            else "EXIT_TRIGGERED_PENDING_CONFIRMATION"
        )
        self._mark_option_execution_route_pending(trade, reason)
        opt.setdefault("event_history", []).append(dict(opt["exit_trigger"]))
        previous_monitoring = opt.get("monitoring_status", "ACTIVE")
        _, current_monitoring = self._set_option_monitoring_health(
            opt, "OPTION_QUOTE", unavailable=False)
        update = ("MONITORING_RESTORED"
                  if previous_monitoring == "UNAVAILABLE" and current_monitoring == "ACTIVE" else None)
        await self._record_trade_update(trade, None, update, quote_at.isoformat())
        return True, (reason if opt.get("execution_route_pending")
                      and opt.get("execution_route_reason") == reason else None)

    async def _process_option_quotes_at_event(self, event_at, quotes):
        events = []
        for trade in self.active_trades:
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            if (opt.get("policy_version") != "option_premium_protection_v2"
                    or trade.get("state") in {"CLOSED", "STOPPED_OUT"}):
                continue
            if opt.get("deferred_option_exit"):
                continue
            contract = opt.get("contract") or {}
            for quote in quotes:
                if trade.get("state") in {"CLOSED", "STOPPED_OUT"}:
                    break
                if str(quote.get("contract", {}).get("security_id")) != str(contract.get("security_id")):
                    continue
                try:
                    observed_at = parse_candle_timestamp(quote["observed_at"])
                    received_at = parse_candle_timestamp(quote["received_at"])
                    price = float(quote["price"])
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    continue
                if (not isfinite(price) or price <= 0 or received_at < observed_at
                        or (received_at - observed_at).total_seconds() > 5):
                    continue
                raw_contract = quote.get("contract") or {}
                if (not _valid_pinned_option_quote_contract(raw_contract, observed_at)
                        or not _valid_pinned_option_quote_contract(contract, observed_at)):
                    continue
                expected_contract = {
                    "security_id": str(contract.get("security_id")),
                    "exchange_segment": contract.get("exchange_segment"),
                    "expiry": contract.get("expiry"), "strike": contract.get("strike"),
                    "option_type": contract.get("option_type"),
                }
                incoming = {"security_id": str(raw_contract.get("security_id")),
                            "exchange_segment": raw_contract.get("exchange_segment"),
                            "expiry": raw_contract.get("expiry"), "strike": raw_contract.get("strike"),
                            "option_type": raw_contract.get("option_type")}
                if incoming != expected_contract:
                    continue
                if self._option_quote_is_replay(opt, observed_at, price):
                    await self._retain_t2_tied_quote_if_protective(
                        trade, observed_at, received_at, price,
                        quote.get("source"), events)
                    continue
                if await self._reject_quote_before_active_stop(
                        trade, observed_at, received_at, price=price,
                        source=quote.get("source"), events=events):
                    # This quote predates the currently active stop revision.
                    # Do not apply a later stop retroactively across monitor batches.
                    continue
                entry_at = parse_candle_timestamp(opt.get("activated_at") or trade.get("entry_timestamp"))
                if observed_at <= entry_at:
                    continue
                t1_at = parse_candle_timestamp(opt["t1_at"]) if opt.get("t1_at") else None
                if t1_at is not None and observed_at == t1_at:
                    retained_exit = self._retained_t1_premium_exit(opt)
                    prior_quote = opt.get("ambiguous_premium_quote")
                    prior_uncertainty = self._unresolved_t1_premium_ambiguity(opt)
                    opt["quote_watermark"] = observed_at.isoformat()
                    quote_source = quote.get("source") or "OPTION_QUOTE"
                    opt["last_option_quote"] = {
                        "price": price, "observed_at": observed_at.isoformat(),
                        "received_at": received_at.isoformat(), "source": quote_source,
                    }
                    original_stop = float(opt["original_option_sl"])
                    if prior_quote and prior_uncertainty is prior_quote and (retained_exit or price > original_stop):
                        await self._record_trade_update(trade, None, None, observed_at.isoformat())
                        continue
                    ambiguous_quote = {
                        "event_at": observed_at.isoformat(), "price": price,
                        "status": "T1_ORDER_UNKNOWN", "received_at": received_at.isoformat(),
                        "source": quote_source,
                    }
                    if prior_quote and prior_uncertainty is prior_quote:
                        ambiguous_quote["prior_ambiguity"] = deepcopy(prior_quote)
                    opt["ambiguous_premium_quote"] = ambiguous_quote
                    if retained_exit:
                        # A same-time quote is additional ambiguous evidence;
                        # it must not replace the first retained bar trigger.
                        ambiguous_quote["protective_option_exit"] = dict(retained_exit)
                        await self._record_trade_update(trade, None, None, observed_at.isoformat())
                        continue
                    entry_premium = float(opt["original_option_entry"])
                    if original_stop < price <= entry_premium:
                        ambiguous_quote.update(
                            ordering_reason="COST_STOP_ONLY_BREACH",
                            original_option_sl=original_stop,
                            entry_premium=entry_premium,
                        )
                    if price <= original_stop:
                        # Under either source ordering the original pre-T1
                        # premium stop has been breached. Preserve the T1
                        # allocation ambiguity, but route paper protection now.
                        source = quote_source
                        protective_exit = {
                            "reason": "SL_HIT", "stop_domain": "OPTION_PREMIUM",
                            "source": source, "trigger_source": source,
                            "event_at": observed_at.isoformat(),
                            "received_at": received_at.isoformat(),
                            "option_close": price, "trigger_price": price,
                            "option_stop": original_stop, "effective_stop": original_stop,
                            "stop_source": "INITIAL_OPTION_SL",
                        }
                        ambiguous_quote["protective_option_exit"] = protective_exit
                        opt["exit_trigger"] = {
                            "domain": "OPTION_PREMIUM", "source": source,
                            "event_at": observed_at.isoformat(), "received_at": received_at.isoformat(),
                            "price": price, "effective_stop": original_stop,
                            "stop_source": "INITIAL_OPTION_SL", "reason": "SL_HIT",
                        }
                        opt["execution_status"] = (
                            "REFERENCE_TRIGGERED_NO_BROKER_FILL"
                            if opt.get("execution_status") in {"REFERENCE_ONLY", "REFERENCE_TRIGGERED_NO_BROKER_FILL"}
                            else "EXIT_TRIGGERED_PENDING_CONFIRMATION"
                        )
                        self._mark_option_execution_route_pending(trade, "SL_HIT")
                        opt.setdefault("event_history", []).append(dict(opt["exit_trigger"]))
                        if opt.get("execution_route_pending"):
                            events.append((trade["id"], "SL_HIT"))
                    await self._record_trade_update(trade, None, None, observed_at.isoformat())
                    continue
                if t1_at is not None and observed_at < t1_at:
                    # A delayed pre-T1 quote cannot be reinterpreted against the cost stop.
                    phase = "PRE_T1"
                else:
                    phase = opt.get("phase")
                if phase in {"POST_T1", "POST_T2"}:
                    cost = float(opt["original_option_entry"])
                    current = float(opt.get("locked_option_sl") or opt["original_option_sl"])
                    if current < cost:
                        opt.update(previous_locked_option_sl=current, locked_option_sl=cost,
                                   protection_mode="BREAK_EVEN_COST", premium_stop_source="BREAK_EVEN_COST",
                                   stop_effective_at=observed_at.isoformat())
                        await self._record_trade_update(trade, None, None, observed_at.isoformat())
                        active_stop = cost
                active_stop = opt.get("locked_option_sl") or opt.get("original_option_sl")
                if active_stop is None:
                    continue
                opt["quote_watermark"] = observed_at.isoformat()
                previous_monitoring = opt.get("monitoring_status", "ACTIVE")
                opt["last_option_quote"] = {
                    "price": price, "observed_at": observed_at.isoformat(),
                    "received_at": received_at.isoformat(), "source": quote.get("source"),
                }
                if (phase == "PRE_T1"
                        and float(opt["original_option_sl"]) < price <= float(opt["original_option_entry"])):
                    pending = opt.setdefault("pending_t1_cost_quotes", [])
                    if not any(isinstance(saved, dict) and saved.get("observed_at") == observed_at.isoformat()
                               for saved in pending):
                        pending.append({**opt["last_option_quote"], "contract": dict(contract)})
                if opt.get("policy_version") == "option_premium_protection_v2":
                    _, current_monitoring = self._set_option_monitoring_health(
                        opt, "OPTION_QUOTE", unavailable=False)
                else:
                    opt["monitoring_status"] = "ACTIVE"
                    opt["monitoring_detail"] = None
                    current_monitoring = "ACTIVE"
                restored = previous_monitoring == "UNAVAILABLE" and current_monitoring == "ACTIVE"
                if phase == "PRE_T1" and price <= float(opt["original_option_sl"]):
                    reason = await self._close_option_premium_trade(
                        trade, price, observed_at, source=quote.get("source") or "OPTION_QUOTE",
                        received_at=received_at.isoformat(), phase_override="PRE_T1",
                        stop_override=opt.get("original_option_sl"))
                    if reason:
                        events.append((trade["id"], reason))
                elif phase in {"POST_T1", "POST_T2"} and price <= float(active_stop):
                    raise_tie = self._atr_raise_quote_stop_basis(opt, observed_at, price)
                    close_options = {}
                    if raise_tie:
                        close_options = {
                            "stop_override": raise_tie["previous_stop"],
                            "stop_source_override": raise_tie["previous_stop_source"],
                            "stop_ordering_status": "ATR_RAISE_TIED",
                            "raised_stop": raise_tie["raised_stop"],
                        }
                    reason = await self._close_option_premium_trade(
                        trade, price, observed_at, source=quote.get("source") or "OPTION_QUOTE",
                        received_at=received_at.isoformat(), **close_options)
                    if reason:
                        events.append((trade["id"], reason))
                else:
                    await self._record_trade_update(
                        trade, None, "MONITORING_RESTORED" if restored else None, observed_at.isoformat())
        return events

    @staticmethod
    def _set_option_monitoring_health(opt, source, *, unavailable, detail=None):
        """Combine quote and ATR-history health without allowing either to mask the other."""
        previous = opt.get("monitoring_status", "ACTIVE")
        sources = list(dict.fromkeys(opt.get("monitoring_unavailable_sources") or []))
        details = dict(opt.get("monitoring_unavailable_details") or {})
        if previous == "UNAVAILABLE" and not sources:
            sources.append("UNCLASSIFIED")
            details["UNCLASSIFIED"] = opt.get("monitoring_detail") or "Previous monitoring outage"
        if unavailable:
            if source not in sources:
                sources.append(source)
            details[source] = str(detail or f"{source} unavailable")[:240]
        else:
            sources = [item for item in sources if item != source]
            details.pop(source, None)
            if source == "OPTION_QUOTE":
                # A fresh quote is the only evidence that can clear an older
                # unclassified outage persisted before source tracking existed.
                sources = [item for item in sources if item != "UNCLASSIFIED"]
                details.pop("UNCLASSIFIED", None)
        opt["monitoring_unavailable_sources"] = sources
        opt["monitoring_unavailable_details"] = details
        opt["monitoring_status"] = "UNAVAILABLE" if sources else "ACTIVE"
        opt["monitoring_detail"] = "; ".join(details[key] for key in sources if details.get(key)) or None
        return previous, opt["monitoring_status"]

    async def mark_option_quote_unavailable(self, contracts, detail, observed_at=None):
        """Persist a visible quote-monitoring gap while retaining each active stop."""
        stamp = parse_candle_timestamp(observed_at or datetime.now(timezone.utc)).isoformat()
        requested = set(map(str, (contracts or {}).keys()))
        for trade in self.active_trades:
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            contract = opt.get("contract") or {}
            if (opt.get("policy_version") != "option_premium_protection_v2"
                    or trade.get("state") in {"CLOSED", "STOPPED_OUT"}
                    or str(contract.get("security_id")) not in requested):
                continue
            previous_sources = list(opt.get("monitoring_unavailable_sources") or [])
            previous_details = dict(opt.get("monitoring_unavailable_details") or {})
            if ("OPTION_QUOTE" in previous_sources
                    and previous_details.get("OPTION_QUOTE") == str(detail)[:240]):
                continue
            previous, current = self._set_option_monitoring_health(
                opt, "OPTION_QUOTE", unavailable=True, detail=detail)
            update = "MONITORING_UNAVAILABLE" if previous != "UNAVAILABLE" and current == "UNAVAILABLE" else None
            await self._record_trade_update(trade, None, update, stamp)

    async def update_option_atr_trailing(self, candles, current_time=None, *, _check_monitoring=True):
        """Process native completed premium bars; old stop is checked before a raise."""
        now = current_time or datetime.now(timezone.utc)
        histories = self._prepare_option_history(candles, now)
        if not getattr(self, "_option_replaying", False):
            self._option_available_until = {key: parse_candle_timestamp(bars[-1].end_time) for key, bars in histories.items() if bars}
        events = []
        await self.retry_option_lifecycle_writes(defer_for_protection=True)
        for trade in self.active_trades:
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            if not _has_option_policy(opt) or trade["state"] in {"CLOSED", "STOPPED_OUT"}:
                continue
            v2 = opt.get("policy_version") == "option_premium_protection_v2"
            bars = histories.get(trade["id"], [])
            phase = opt.get("phase")
            if phase not in {"POST_T1", "POST_T2"}:
                continue
            cutoff = parse_candle_timestamp(opt.get("last_processed_option_bar_end") or opt["t1_at"])
            if opt.get("locked_option_sl") is None:
                self._seed_option_stop(trade, parse_candle_timestamp(opt["t1_at"]))
            for bar in bars:
                end = parse_candle_timestamp(bar.end_time)
                pending_exits = [opt.get("deferred_option_exit"),
                                 (opt.get("ambiguous_index_touch") or {}).get("protective_option_exit"),
                                 self._retained_t1_premium_exit(opt)]
                pending_exits = [item for item in pending_exits if item]
                try:
                    if any(end >= parse_candle_timestamp(item["event_at"]) for item in pending_exits):
                        # Replay completed option bars strictly before the retained
                        # trigger; never let later history rewrite its chronology.
                        break
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    # Missing trigger time cannot establish bar ordering.
                    break
                if end < cutoff or (end == cutoff and opt.get("last_processed_option_bar_end")):
                    continue
                t1_at = parse_candle_timestamp(opt["t1_at"]) if opt.get("t1_at") else None
                if v2 and end == t1_at:
                    reason = await self._retain_t1_premium_bar_exit(trade, bar)
                    if reason:
                        events.append((trade["id"], reason))
                    if self._retained_t1_premium_exit(opt):
                        break
                    if await self._retain_t1_premium_bar_ambiguity(trade, bar):
                        break
                opt["source_candle"] = {"start_time": bar.start_time, "end_time": bar.end_time, "close": bar.close}
                opt["last_valid_option_bar_end"] = bar.end_time
                active_stop = opt.get("locked_option_sl")
                if active_stop is None and phase == "POST_T2":
                    active_stop = opt["original_option_sl"]
                opt["last_processed_option_bar_end"] = bar.end_time
                if active_stop is not None and (bar.close <= active_stop if v2 else bar.close < active_stop):
                    if v2:
                        reason = await self._close_option_premium_trade(
                            trade, bar.close, end, source="completed_5m_option_close")
                        if reason:
                            events.append((trade["id"], reason))
                        break
                    reason = "ATR_SL_HIT" if opt.get("protection_mode") == "ATR" else "OPTION_SL_FLOOR_HIT"
                    remainder = opt["remaining_fraction"]
                    if opt.get("ambiguous_index_touch"):
                        opt["ambiguous_index_touch"].setdefault("protective_option_exit", {
                            "event_at": bar.end_time, "option_close": bar.close,
                            "option_stop": active_stop, "reason": reason,
                        })
                        await self._record_trade_update(trade, None, None, bar.end_time)
                        break
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
                    if opt.get("accounting_formula_version") == "one_lot_reference_v1":
                        self._capture_reference_leg(trade, "EXIT", None, bar.end_time, option_close=bar.close)
                        index_price = opt["accounting"]["legs"]["EXIT"].get("spot_price")
                        opt["exit"]["index_price"] = index_price
                        opt["exit"]["index_price_at"] = opt["accounting"]["legs"]["EXIT"].get("spot_observed_at")
                    await self._record_trade_update(trade, index_price, reason, bar.end_time)
                    events.append((trade["id"], reason))
                    break
                opt["raw_line"] = bar.raw_line
                if v2 and not opt.get("atr_enabled"):
                    await self._record_trade_update(trade, None, None, bar.end_time)
                    continue
                old = opt.get("locked_option_sl")
                stop, mode, raised = ratchet_option_stop(
                    old, bar.raw_line, opt["original_option_sl"],
                    protection_mode=opt.get("protection_mode", "NONE"),
                    entry_premium_floor=opt.get("original_option_entry") if v2 else None,
                )
                update = None
                if raised:
                    update = "ATR_ACTIVE" if opt.get("protection_mode") != "ATR" and mode == "ATR" else "OPTION_SL_RATCHETED"
                    opt.update(previous_locked_option_sl=old, locked_option_sl=stop, protection_mode=mode,
                               atr_eligible=mode == "ATR", stop_effective_at=bar.end_time,
                               premium_stop_source="ATR_TRAILING" if v2 and mode == "ATR" else opt.get("premium_stop_source"))
                await self._record_trade_update(trade, None, update, bar.end_time)
                if update:
                    events.append((trade["id"], update))
            if trade["state"] == "CLOSED":
                continue
            if not _check_monitoring or (v2 and opt.get("atr_enabled") is not True):
                continue
            expected = expected_completed_option_bar_end(now)
            valid = parse_candle_timestamp(bars[-1].end_time) if bars else None
            unavailable = expected is not None and expected.date() in self._option_confirmed_sessions and now > expected + timedelta(seconds=60) and (valid is None or valid < expected)
            if v2:
                previous_sources = list(opt.get("monitoring_unavailable_sources") or [])
                previous_details = dict(opt.get("monitoring_unavailable_details") or {})
                recovered = (not unavailable and valid is not None and expected is not None and valid >= expected)
                if unavailable:
                    previous, current = self._set_option_monitoring_health(
                        opt, "OPTION_HISTORY", unavailable=True,
                        detail="Missing, invalid, stale, or discontinuous pinned option history; stop retained")
                elif recovered:
                    previous, current = self._set_option_monitoring_health(
                        opt, "OPTION_HISTORY", unavailable=False)
                else:
                    previous = current = opt.get("monitoring_status", "ACTIVE")
                sources_changed = (previous_sources != opt.get("monitoring_unavailable_sources")
                                   or previous_details != opt.get("monitoring_unavailable_details"))
                if previous != "UNAVAILABLE" and current == "UNAVAILABLE":
                    update = "MONITORING_UNAVAILABLE"
                elif previous == "UNAVAILABLE" and current == "ACTIVE":
                    update = "MONITORING_RESTORED"
                else:
                    update = None
                if update or sources_changed:
                    await self._record_trade_update(trade, None, update, now.isoformat())
            else:
                status = opt.get("monitoring_status", "ACTIVE")
                if unavailable and status != "UNAVAILABLE":
                    opt["monitoring_status"] = "UNAVAILABLE"
                    opt["monitoring_detail"] = "Missing, invalid, stale, or discontinuous pinned option history; stop retained"
                    await self._record_trade_update(trade, None, "MONITORING_UNAVAILABLE", now.isoformat())
                elif not unavailable and valid is not None and expected is not None and valid >= expected and status == "UNAVAILABLE":
                    opt["monitoring_status"] = "RESTORED"
                    await self._record_trade_update(trade, None, "MONITORING_RESTORED", now.isoformat())
        return events

    async def update_option_market_events(self, index_candles, option_histories, current_time=None, *, index_ticks=None,
                                          option_quotes=None, quote_freshness_at=None):
        """Merge spot events, pinned premium quotes, and completed option bars by source time.

        ``quote_freshness_at`` anchors receipt-age validation to the quote batch
        fetch, so later option-history I/O cannot make a fresh stop observation stale.
        """
        now = current_time or datetime.now(timezone.utc)
        events = []
        quote_reference = now
        if quote_freshness_at is not None:
            try:
                quote_reference = parse_candle_timestamp(quote_freshness_at)
            except (TypeError, ValueError, OverflowError, OSError):
                quote_reference = now
        histories = self._prepare_option_history(option_histories, now)
        self._option_available_until = {key: parse_candle_timestamp(bars[-1].end_time) for key, bars in histories.items() if bars}
        timeline = {}
        premium_quotes = {}
        option_trades = [trade for trade in self.active_trades
                         if trade.get("state") not in {"CLOSED", "STOPPED_OUT"}
                         and _has_option_policy((trade.get("market_context") or {}).get("option_trailing") or {})]
        for quote in option_quotes or []:
            if not isinstance(quote, dict):
                continue
            try:
                observed_at = parse_candle_timestamp(quote["observed_at"])
                received_at = parse_candle_timestamp(quote["received_at"])
                age = (parse_candle_timestamp(quote_reference) - received_at).total_seconds()
                source_lag = (received_at - observed_at).total_seconds()
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                continue
            if not (0 <= age <= 5) or not (0 <= source_lag <= 5):
                continue
            try:
                value = float(quote["price"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if isinstance(quote.get("price"), bool) or not isfinite(value) or value <= 0:
                continue
            raw_contract = quote.get("contract") or {}
            if not _valid_pinned_option_quote_contract(raw_contract, observed_at):
                continue
            incoming_contract = {
                "security_id": str(raw_contract.get("security_id")),
                "exchange_segment": raw_contract.get("exchange_segment"),
                "expiry": raw_contract.get("expiry"), "strike": raw_contract.get("strike"),
                "option_type": raw_contract.get("option_type"),
            }
            matching = []
            for trade in option_trades:
                opt = (trade.get("market_context") or {}).get("option_trailing") or {}
                if opt.get("policy_version") != "option_premium_protection_v2":
                    continue
                expected = opt.get("contract") or {}
                if not _valid_pinned_option_quote_contract(expected, observed_at):
                    continue
                expected_contract = {
                    "security_id": str(expected.get("security_id")),
                    "exchange_segment": expected.get("exchange_segment"),
                    "expiry": expected.get("expiry"), "strike": expected.get("strike"),
                    "option_type": expected.get("option_type"),
                }
                if incoming_contract != expected_contract:
                    continue
                if self._option_quote_is_replay(opt, observed_at, value):
                    await self._retain_t2_tied_quote_if_protective(
                        trade, observed_at, received_at, value,
                        quote.get("source"), events)
                    continue
                if await self._reject_quote_before_active_stop(
                        trade, observed_at, received_at, price=value,
                        source=quote.get("source"), events=events):
                    continue
                entry_at = parse_candle_timestamp(opt.get("activated_at") or trade.get("entry_timestamp"))
                if observed_at <= entry_at:
                    continue
                matching.append(trade)
            if not matching:
                continue
            # A shared contract quote can be valid for one trade but stale for
            # another whose stop was raised at a later source time. Preserve
            # the per-trade decision for the same-close detector below.
            premium_quotes.setdefault(observed_at, []).append({
                **quote, "_accepted_trade_ids": tuple(str(trade["id"]) for trade in matching),
            })
            timeline.setdefault(observed_at, [])
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
            deferred_exit = opt.get("deferred_option_exit")
            if deferred_exit:
                try:
                    timeline.setdefault(parse_candle_timestamp(deferred_exit["event_at"]), [])
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    pass
        pending_ticks = {}
        live_ticks = {}
        for tick in index_ticks or []:
            if not isinstance(tick, dict):
                continue
            try:
                price = float(tick["price"])
                received_at = parse_candle_timestamp(tick.get("received_at") or tick["exchange_at"])
                source_at = tick.get("exchange_at")
                event_at = parse_candle_timestamp(source_at) if source_at is not None else received_at
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                continue
            if not isfinite(price) or price <= 0:
                continue
            if source_at is not None and event_at > received_at + timedelta(seconds=5):
                source_at = None
                event_at = received_at
            if source_at is None and not 0 <= (now - received_at).total_seconds() <= 5:
                # A stale fallback may still protect legacy spot trades, but
                # cannot advance option-policy lifecycle state.
                continue
            live_ticks.setdefault(event_at, []).append({
                "price": price, "exchange_at": source_at,
                "received_at": received_at,
            })
            timeline.setdefault(event_at, [])
        for trade in self.active_trades:
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            if not _has_option_policy(opt):
                continue
            queued = list(opt.get("pending_index_observations") or [])
            for observation in queued:
                event_at = parse_candle_timestamp(observation["event_at"])
                timeline.setdefault(event_at, [])
                pending_ticks.setdefault(event_at, []).append((trade["id"], observation))
            # If unavailable, update_trades requeues the observation below.
            opt["pending_index_observations"] = []
        self._option_replaying = True
        try:
            return await self._replay_option_timeline(
                timeline, pending_ticks, live_ticks, histories, option_histories, now, events,
                premium_quotes=premium_quotes)
        finally:
            self._option_replaying = False

    async def _mark_same_close_option_spot_ambiguities(
            self, event_at, timeline, pending_ticks, live_ticks, histories, premium_quotes=None):
        """Preserve spot/option ordering when a premium trigger ties an index touch."""
        events = []
        for trade in self.active_trades:
            opt = (trade.get("market_context") or {}).get("option_trailing") or {}
            v2 = opt.get("policy_version") == "option_premium_protection_v2"
            if (not _has_option_policy(opt)
                    or (not v2 and opt.get("accounting_formula_version") != "one_lot_reference_v1")
                    or opt.get("ambiguous_index_touch")):
                continue
            execution = ((trade.get("market_context") or {}).get("execution") or {})
            activation_value = (opt.get("activated_at") or execution.get("filled_at")
                                or trade.get("entry_timestamp"))
            if activation_value:
                try:
                    activation_at = parse_candle_timestamp(activation_value)
                except (TypeError, ValueError, OverflowError, OSError):
                    # An invalid activation timestamp cannot authorize a protective
                    # event against a position whose start time is unknown.
                    continue
                if event_at < activation_at:
                    continue
                if event_at.microsecond == 0 and event_at + timedelta(seconds=1) <= activation_at:
                    continue
            last_index = opt.get("last_index_event_at")
            if last_index:
                try:
                    if event_at < parse_candle_timestamp(last_index):
                        continue
                except (TypeError, ValueError, OverflowError, OSError):
                    continue
            expected = opt.get("contract") or {}
            accepted_quotes = []
            candidates = [(candidate, False) for candidate in (premium_quotes or {}).get(event_at, [])]
            if v2 and trade.get("state") == "OPEN" and opt.get("phase") == "PRE_T1" and not opt.get("t1_at"):
                pending = []
                for saved in opt.get("pending_t1_cost_quotes") or []:
                    try:
                        saved_at = parse_candle_timestamp(saved["observed_at"])
                    except (KeyError, TypeError, ValueError, OverflowError, OSError):
                        continue
                    if not last_index or saved_at >= parse_candle_timestamp(last_index):
                        pending.append(saved)
                opt["pending_t1_cost_quotes"] = pending
                cached = list(pending)
                last_quote = opt.get("last_option_quote")
                if isinstance(last_quote, dict):
                    cached.append({"contract": dict(expected), **last_quote})
                candidates = [(saved, True) for saved in cached] + candidates
            for candidate, previously_accepted in candidates:
                accepted_trade_ids = candidate.get("_accepted_trade_ids")
                if (not previously_accepted and (not isinstance(accepted_trade_ids, (tuple, list, set))
                        or str(trade.get("id")) not in {str(value) for value in accepted_trade_ids})):
                    continue
                try:
                    quote_at = parse_candle_timestamp(candidate["observed_at"])
                    received_at = parse_candle_timestamp(candidate["received_at"])
                    raw_contract = candidate.get("contract") or {}
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    continue
                if (quote_at != event_at
                        or not _valid_pinned_option_quote_contract(raw_contract, quote_at)
                        or not _valid_pinned_option_quote_contract(expected, quote_at)):
                    continue
                if any(str(raw_contract.get(key)) != str(expected.get(key))
                       for key in ("security_id", "exchange_segment", "expiry", "strike", "option_type")):
                    continue
                if not previously_accepted and self._option_quote_is_replay(opt, quote_at, candidate.get("price")):
                    await self._retain_t2_tied_quote_if_protective(
                        trade, quote_at, received_at, candidate.get("price"),
                        candidate.get("source"), events)
                    continue
                try:
                    quote_price = float(candidate["price"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if (not isfinite(quote_price) or quote_price <= 0
                        or not 0 <= (received_at - quote_at).total_seconds() <= 5):
                    continue
                if previously_accepted and not float(opt["original_option_sl"]) < quote_price <= float(opt["original_option_entry"]):
                    continue
                if await self._reject_quote_before_active_stop(
                        trade, quote_at, received_at, price=quote_price,
                        source=candidate.get("source"), events=events):
                    continue
                try:
                    entry_at = parse_candle_timestamp(opt.get("activated_at") or trade.get("entry_timestamp"))
                except (TypeError, ValueError, OverflowError, OSError):
                    continue
                if quote_at <= entry_at:
                    continue
                accepted_quotes.append({**candidate, "_previously_accepted": previously_accepted})
            bar = next((candidate for candidate in histories.get(trade["id"], [])
                        if parse_candle_timestamp(candidate.end_time) == event_at), None)
            bar_watermark = opt.get("last_processed_option_bar_end")
            if bar is not None and bar_watermark:
                try:
                    if event_at <= parse_candle_timestamp(bar_watermark):
                        bar = None
                except (TypeError, ValueError, OverflowError, OSError):
                    bar = None
            spot_ranges = []
            for candle in timeline.get(event_at, []):
                start = parse_candle_timestamp(candle["timestamp"])
                if start + timedelta(minutes=1) == event_at:
                    spot_ranges.append((candle["high"], candle["low"], start,
                                        "completed_candle", candle["close"]))
            for trade_id, observation in pending_ticks.get(event_at, []):
                if trade_id == trade["id"]:
                    spot_ranges.append((observation.get("high", observation["price"]),
                                        observation.get("low", observation["price"]),
                                        parse_candle_timestamp(observation.get("candle_timestamp") or event_at),
                                        "recovered_observation", observation["price"]))
            ticks = live_ticks.get(event_at, [])
            if ticks:
                prices = [tick["price"] for tick in ticks]
                spot_ranges.append((max(prices), min(prices), event_at, "live_tick", None))

            # Mirror update_trades' entry-boundary treatment: a completed candle
            # that began before activation contributes only its close, never its
            # pre-entry wick. A buffered source-second wholly before activation
            # contributes no index range at all.
            eligible_ranges = []
            for high, low, source_start, source, close in spot_ranges:
                if activation_value:
                    if source == "live_tick":
                        if source_start + timedelta(seconds=1) <= activation_at:
                            continue
                    elif source_start < activation_at:
                        high = low = close
                eligible_ranges.append((high, low, source_start, source, close))
            spot_ranges = eligible_ranges

            # The detector runs before index milestones. Capture a T1/bar tie
            # here while the trade is still PRE_T1, using only the original SL.
            if (v2 and trade.get("state") == "OPEN" and opt.get("phase") == "PRE_T1"
                    and not opt.get("t1_at") and bar is not None and event_at.microsecond == 0
                    and not self._retained_t1_premium_exit(opt)):
                try:
                    original_stop = float(opt["original_option_sl"])
                    bar_close = float(bar.close)
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                entry_premium = float(opt.get("original_option_entry") or 0)
                if isfinite(bar_close) and 0 < bar_close <= entry_premium:
                    sign = 1 if trade["direction"] == "BULLISH" else -1
                    tied_index_observations = []
                    for high, low, source_start, source, close in spot_ranges:
                        touches_t1 = high >= trade["target_1"] if sign == 1 else low <= trade["target_1"]
                        if not touches_t1:
                            continue
                        touches_t2 = high >= trade["target_2"] if sign == 1 else low <= trade["target_2"]
                        touches_guard = low <= trade["entry_price"] if sign == 1 else high >= trade["entry_price"]
                        tied_index_observations.append({
                            "event_at": event_at.isoformat(), "price": close,
                            "high": high, "low": low, "source": source,
                            "touches_t1": True, "touches_t2": touches_t2,
                            "touches_guard": touches_guard,
                            "candle_timestamp": source_start.isoformat()
                                if source != "live_tick" else None,
                        })
                    if tied_index_observations:
                        if bar_close <= original_stop:
                            reason = await self._retain_t1_premium_bar_exit(trade, bar)
                            if reason:
                                events.append((trade["id"], reason))
                        else:
                            await self._retain_t1_premium_bar_ambiguity(
                                trade, bar, tied_index_observations)

            if (v2 and trade.get("state") == "OPEN" and opt.get("phase") == "PRE_T1"
                    and not opt.get("t1_at") and not self._retained_t1_premium_exit(opt)
                    and spot_ranges):
                sign = 1 if trade["direction"] == "BULLISH" else -1
                t1_ranges = []
                for high, low, source_start, source, close in spot_ranges:
                    touches_t1 = high >= trade["target_1"] if sign == 1 else low <= trade["target_1"]
                    if touches_t1:
                        touches_t2 = high >= trade["target_2"] if sign == 1 else low <= trade["target_2"]
                        touches_guard = low <= trade["entry_price"] if sign == 1 else high >= trade["entry_price"]
                        t1_ranges.append({
                            "event_at": event_at.isoformat(), "price": close,
                            "high": high, "low": low, "source": source,
                            "touches_t1": touches_t1, "touches_t2": touches_t2,
                            "touches_guard": touches_guard,
                            "candle_timestamp": source_start.isoformat()
                                if source != "live_tick" else None,
                        })
                if t1_ranges:
                    for candidate in accepted_quotes:
                        try:
                            quote_at = parse_candle_timestamp(candidate["observed_at"])
                            quote_price = float(candidate["price"])
                            original_stop = float(opt["original_option_sl"])
                            entry_premium = float(opt["original_option_entry"])
                            raw_contract = candidate.get("contract") or {}
                        except (KeyError, TypeError, ValueError, OverflowError, OSError):
                            continue
                        if (quote_at != event_at or quote_price > entry_premium
                                or str(raw_contract.get("security_id")) != str(expected.get("security_id"))
                                or raw_contract.get("exchange_segment") != expected.get("exchange_segment")
                                or raw_contract.get("expiry") != expected.get("expiry")
                                or raw_contract.get("strike") != expected.get("strike")
                                or raw_contract.get("option_type") != expected.get("option_type")):
                            continue
                        if quote_price <= original_stop:
                            reason = await self._retain_t1_premium_quote_exit(
                                trade, candidate, event_at, t1_ranges)
                            if reason:
                                events.append((trade["id"], reason))
                        else:
                            await self._retain_t1_premium_quote_ambiguity(
                                trade, candidate, event_at, t1_ranges,
                                previously_accepted=candidate.get("_previously_accepted") is True)
                        break

            if (trade.get("state") != "T1_HIT" or opt.get("phase") != "POST_T1"
                    or not opt.get("t1_at")):
                continue
            try:
                if event_at <= parse_candle_timestamp(opt["t1_at"]):
                    continue
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            # The option-bar watermark filters `bar` above. Do not use it to
            # suppress a fresh premium quote at the same event time: quote
            # freshness, contract identity, stop-effective time, and duplicate
            # handling are validated independently when quotes are ingested.
            active_stop = opt.get("locked_option_sl")
            if active_stop is None:
                continue
            try:
                active_stop = float(active_stop)
            except (TypeError, ValueError, OverflowError):
                continue

            quote_trigger = None
            if v2:
                for candidate in accepted_quotes:
                    try:
                        quote_at = parse_candle_timestamp(candidate["observed_at"])
                        quote_price = float(candidate["price"])
                        raw_contract = candidate.get("contract") or {}
                    except (KeyError, TypeError, ValueError, OverflowError, OSError):
                        continue
                    if quote_at != event_at or quote_price > active_stop:
                        continue
                    if (str(raw_contract.get("security_id")) != str(expected.get("security_id"))
                            or raw_contract.get("exchange_segment") != expected.get("exchange_segment")
                            or raw_contract.get("expiry") != expected.get("expiry")
                            or raw_contract.get("strike") != expected.get("strike")
                            or raw_contract.get("option_type") != expected.get("option_type")):
                        continue
                    quote_trigger = {
                        **candidate,
                        "_atr_raise_tie": self._atr_raise_quote_stop_basis(
                            opt, quote_at, quote_price),
                    }
                    break

            bar_breaches = (bar is not None and event_at.microsecond == 0
                            and (bar.close <= active_stop if v2 else bar.close < active_stop))
            if not bar_breaches and quote_trigger is None:
                continue

            sign = 1 if trade["direction"] == "BULLISH" else -1
            for high, low, source_start, source, _ in spot_ranges:
                touches_t2 = high >= trade["target_2"] if sign == 1 else low <= trade["target_2"]
                touches_guard = low <= trade["entry_price"] if sign == 1 else high >= trade["entry_price"]
                if not touches_t2 and not touches_guard:
                    continue
                reason = "ATR_SL_HIT" if opt.get("protection_mode") == "ATR" else "OPTION_SL_FLOOR_HIT"
                ambiguity = opt["ambiguous_index_touch"] = {
                    "kind": "SPOT_OPTION_EXIT_TIE",
                    "source_minute": source_start.replace(second=0, microsecond=0).isoformat(),
                    "event_at": __import__("storage").to_utc_iso(event_at),
                    "source": source,
                    "premium_source": (quote_trigger.get("source") or "OPTION_QUOTE")
                        if quote_trigger else "completed_5m_option_close",
                    "high": high,
                    "low": low,
                    "touches_t2": touches_t2,
                    "touches_guard": touches_guard,
                    "status": "ORDER_UNKNOWN",
                }
                if not v2:
                    ambiguity["protective_option_exit"] = {
                        "event_at": __import__("storage").to_utc_iso(bar.end_time),
                        "option_close": bar.close,
                        "option_stop": active_stop,
                        "reason": reason,
                    }
                if v2:
                    if quote_trigger:
                        received_at = parse_candle_timestamp(quote_trigger["received_at"])
                        opt["quote_watermark"] = event_at.isoformat()
                        opt["last_option_quote"] = {
                            "price": float(quote_trigger["price"]),
                            "observed_at": event_at.isoformat(),
                            "received_at": received_at.isoformat(),
                            "source": quote_trigger.get("source"),
                        }
                    raise_tie = quote_trigger.get("_atr_raise_tie") if quote_trigger else None
                    close_options = {}
                    if raise_tie:
                        close_options = {
                            "stop_override": raise_tie["previous_stop"],
                            "stop_source_override": raise_tie["previous_stop_source"],
                            "stop_ordering_status": "ATR_RAISE_TIED",
                            "raised_stop": raise_tie["raised_stop"],
                        }
                    reason = await self._close_option_premium_trade(
                        trade,
                        float(quote_trigger["price"]) if quote_trigger else bar.close,
                        event_at,
                        source=(quote_trigger.get("source") or "OPTION_QUOTE")
                            if quote_trigger else "completed_5m_option_close",
                        received_at=quote_trigger.get("received_at") if quote_trigger else None,
                        **close_options,
                    )
                    if reason:
                        events.append((trade["id"], reason))
                else:
                    await self._record_trade_update(trade, None, None, ambiguity["event_at"])
                break
        return events

    async def _replay_option_timeline(self, timeline, pending_ticks, live_ticks, histories, option_histories, now, events,
                                      premium_quotes=None):
        for event_at in sorted(timeline):
            events.extend(await self._mark_same_close_option_spot_ambiguities(
                event_at, timeline, pending_ticks, live_ticks, histories,
                premium_quotes=premium_quotes))
            for candle in timeline[event_at]:
                val = (lambda key: candle[key]) if isinstance(candle, dict) else (lambda key: getattr(candle, key))
                events.extend(await self.update_trades(val("close"), candle_high=val("high"), candle_low=val("low"),
                    candle_timestamp=parse_candle_timestamp(val("timestamp")), event_time=event_at, only_option_policy=True))
            for trade_id, tick in pending_ticks.get(event_at, []):
                events.extend(await self.update_trades(tick["price"], candle_high=tick["high"], candle_low=tick["low"],
                    candle_timestamp=parse_candle_timestamp(tick["candle_timestamp"]) if tick.get("candle_timestamp") else None,
                    event_time=event_at, only_option_policy=True, only_trade_id=trade_id))
            for tick in live_ticks.get(event_at, []):
                events.extend(await self.update_trades(
                    tick["price"], event_time=tick["received_at"],
                    market_event_time=tick["exchange_at"], only_option_policy=True))
            events.extend(await self._apply_deferred_option_exit_at_event(event_at))
            events.extend(await self._process_option_quotes_at_event(
                event_at, (premium_quotes or {}).get(event_at, [])))
            events.extend(await self.update_option_atr_trailing(option_histories, current_time=event_at, _check_monitoring=False))
        # Staleness is measured against detection time only after chronological recovery.
        events.extend(await self.update_option_atr_trailing(option_histories, current_time=now))
        return events
