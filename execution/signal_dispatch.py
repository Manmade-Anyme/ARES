"""MANM-176: bridge validated paper orders to the existing spot analytics lifecycle.

This runtime deliberately does not enable a live adapter. Broker recovery/session
policy must be provisioned before live order state can own trade registration.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import logging
import math
from types import SimpleNamespace

from config import settings
from execution.order_manager import OrderRecord
from models import OptionGreeks, OptionOrderBracket

logger = logging.getLogger(__name__)


class SignalDispatcher:
    def __init__(self, orders, positions):
        if orders.broker is not None:
            raise ValueError("The application dispatcher currently supports paper execution only")
        self.orders = orders
        self.positions = positions
        self._signals = {}

    async def dispatch(self, signal, spot, full_chain, atm=None):
        if signal.bracket is None or not signal.bracket.is_valid:
            signal.trade_binding_status = "ORDER_REJECTED"
            return False
        duplicate = getattr(self.positions, "find_duplicate_trade", None)
        if duplicate is not None and duplicate(signal, spot):
            signal.trade_binding_status = "DUPLICATE_SKIPPED"
            return False
        for correlation_id, (pending, _) in self._signals.items():
            # Completed fills may remain here solely for persistence retries.
            if self.orders.records[correlation_id].status in {"CANCELLED", "REJECTED", "CLOSED"}:
                continue
            # Restored bindings own an exit only, not an entry candidate.
            if getattr(pending, "exit_only", False):
                continue
            if (pending.id != signal.id and pending.setup_type == signal.setup_type
                    and pending.direction == signal.direction
                    and abs(pending.bracket.spot_entry - spot) <= settings.trade_dedupe_tolerance_pts):
                signal.trade_binding_status = "DUPLICATE_SKIPPED"
                return False
        record = self.orders.dispatch_bracket(signal.bracket)
        self._signals.setdefault(record.correlation_id, (signal, atm))
        await self._observe(record, full_chain, spot)
        return bool(signal.trade_id)

    async def on_snapshot(self, full_chain, spot):
        for correlation_id in tuple(self._signals):
            try:
                await self._observe(self.orders.records[correlation_id], full_chain, spot)
            except Exception:
                logger.exception("Paper order update failed for %s; will retry", correlation_id)

    async def on_spot(self, spot):
        # Pending orders also need invalidation, even without an active trade.
        for correlation_id in tuple(self._signals):
            try:
                record = self.orders.on_spot(correlation_id, spot)
                self._publish(record)
                await self._sync(record)
            except Exception:
                logger.exception("Paper spot update failed for %s; will retry", correlation_id)

    async def on_candles(self, candles):
        """Never use index candles as an ATR source for option execution records."""
        for correlation_id in tuple(self._signals):
            try:
                record = self.orders.records[correlation_id]
                # Every dispatched order has a pinned CE/PE bracket. Its ATR
                # lifecycle consumes option-premium bars through PositionManager.
                if record.bracket and record.bracket.option_type in {"CE", "PE"}:
                    continue
                record = self.orders.on_candles(correlation_id, candles)
                self._publish(record)
                await self._sync(record)
            except Exception:
                logger.exception("Paper candle update failed for %s; will retry", correlation_id)

    async def on_option_stop(self, trade, reason):
        """Route a v2 premium or index-stop closure to paper execution."""
        if not isinstance(trade, dict):
            return False
        trade_signal = str(trade.get("signal_uuid") or trade.get("signal_id") or "")
        option = ((trade.get("market_context") or {}).get("option_trailing") or {})
        contract = option.get("contract") or {}
        exit_state = (option.get("exit")
                      or (option.get("ambiguous_index_touch") or {}).get("protective_option_exit")
                      or (option.get("ambiguous_premium_bar") or {}).get("protective_option_exit")
                      or (option.get("ambiguous_premium_quote") or {}).get("protective_option_exit")
                      or option.get("deferred_option_exit")
                      or {})
        route_pending = (option.get("execution_route_pending")
                         and option.get("execution_route_reason") == reason)
        stop_domain = exit_state.get("stop_domain")
        index_break_even = (stop_domain == "SPOT_INDEX" and reason == "STOPPED_OUT_AT_BE"
                            and option.get("highest_milestone") == 1)
        initial_index_stop = (stop_domain == "SPOT_INDEX" and reason == "SL_HIT"
                              and option.get("highest_milestone") == 0)
        if (not trade_signal or option.get("policy_version") != "option_premium_protection_v2"
                or option.get("execution_status") in {"REFERENCE_ONLY", "REFERENCE_TRIGGERED_NO_BROKER_FILL"}
                or (stop_domain != "OPTION_PREMIUM" and not index_break_even and not initial_index_stop)
                or (exit_state.get("reason") != reason and not route_pending)):
            return False
        if not any(str(signal.id) == trade_signal for signal, _ in self._signals.values()):
            self._restore_option_execution_binding(trade, trade_signal, option)
        for correlation_id, (signal, _) in tuple(self._signals.items()):
            if str(signal.id) != trade_signal:
                continue
            record = self.orders.records[correlation_id]
            bracket = record.bracket
            try:
                same_strike = (not isinstance(contract.get("strike"), bool)
                               and math.isclose(float(contract.get("strike")), float(bracket.strike), abs_tol=1e-9))
            except (TypeError, ValueError, OverflowError):
                same_strike = False
            if not (str(contract.get("security_id")) == str(bracket.security_id)
                    and contract.get("exchange_segment") == "NSE_FNO"
                    and str(contract.get("expiry")) == str(bracket.expiry_date)
                    and same_strike and str(contract.get("option_type")).upper() == bracket.option_type):
                logger.error("Premium stop contract does not match paper execution for trade %s", trade.get("id"))
                return False
            self.orders.request_option_exit(correlation_id, reason)
            self._publish(record)
            return await self._sync(record)
        return False

    def _restore_option_execution_binding(self, trade, trade_signal, option):
        """Rebuild a paper-only route from the execution snapshot after restart."""
        context = trade.get("market_context")
        if not isinstance(context, dict) or not isinstance(option, dict):
            return False
        execution = context.get("execution") or {}
        if (not isinstance(execution, dict) or execution.get("mode") != "PAPER"
                or option.get("execution_status") not in {
                    "CONFIRMED_PAPER_FILL", "EXIT_TRIGGERED_PENDING_CONFIRMATION"}):
            return False
        correlation_id = str(execution.get("correlation_id") or "")
        if not correlation_id:
            return False

        record = self.orders.records.get(correlation_id)
        if record is None:
            saved = execution.get("order_record")
            record = self._record_from_snapshot(correlation_id, saved) if isinstance(saved, dict) else None
            if record is None:
                record = self._record_from_legacy_context(
                    correlation_id, trade, option, execution,
                )
            if record is None:
                logger.error("Pending option exit lacks a recoverable paper execution binding for trade %s",
                             trade.get("id"))
                return False
            self.orders.records[correlation_id] = record

        if (not self._valid_recovered_record(record)
                or record.bracket.signal_id != trade_signal
                or not self._matches_contract(record.bracket, option.get("contract") or {})):
            logger.error("Pending option exit execution binding does not match trade %s", trade.get("id"))
            return False
        signal = SimpleNamespace(
            id=trade_signal,
            trade_id=trade.get("id"),
            market_context=context,
            bracket=record.bracket,
            exit_only=True,
        )
        self._signals.setdefault(correlation_id, (signal, None))
        return True

    @staticmethod
    def _matches_contract(bracket, contract):
        if not isinstance(contract, dict):
            return False
        security_id = str(contract.get("security_id") or "")
        option_type = str(contract.get("option_type") or "").upper()
        try:
            strike_matches = (not isinstance(contract.get("strike"), bool)
                              and math.isclose(float(contract.get("strike")), float(bracket.strike), abs_tol=1e-9))
            datetime.strptime(str(contract.get("expiry")), "%Y-%m-%d")
        except (TypeError, ValueError, OverflowError):
            strike_matches = False
        return bool(
            security_id.isdigit()
            and option_type in {"CE", "PE"}
            and security_id == str(bracket.security_id)
            and contract.get("exchange_segment") == "NSE_FNO"
            and str(contract.get("expiry")) == str(bracket.expiry_date)
            and strike_matches
            and option_type == bracket.option_type
        )

    @staticmethod
    def _record_from_snapshot(correlation_id, snapshot):
        if not isinstance(snapshot, dict) or str(snapshot.get("correlation_id")) != correlation_id:
            return None
        values = dict(snapshot)
        raw_bracket = values.pop("bracket", None)
        if not isinstance(raw_bracket, dict):
            return None
        try:
            bracket_values = dict(raw_bracket)
            greeks = bracket_values.get("greeks")
            if isinstance(greeks, dict):
                bracket_values["greeks"] = OptionGreeks(**greeks)
            elif greeks is not None:
                return None
            bracket = OptionOrderBracket(**bracket_values)
            allowed = {
                "mode", "status", "filled_quantity", "fill_price", "entry_spot",
                "protection_confirmed", "exited_quantity", "realized_pnl",
                "active_spot_sl", "t1_hit", "atr_last_bar", "exit_reason",
                "pending_exit_quantity", "slices", "rejection_reason",
            }
            record = OrderRecord(
                correlation_id=correlation_id,
                bracket=bracket,
                **{key: value for key, value in values.items() if key in allowed},
            )
        except (TypeError, ValueError):
            return None
        return record if SignalDispatcher._valid_recovered_record(record) else None

    @staticmethod
    def _record_from_legacy_context(correlation_id, trade, option, execution):
        """Read pre-order_record snapshots without inventing a live execution."""
        if (execution.get("mode") != "PAPER" or execution.get("protection_confirmed") is not True
                or option.get("execution_status") not in {
                    "CONFIRMED_PAPER_FILL", "EXIT_TRIGGERED_PENDING_CONFIRMATION"}):
            return None
        context = trade.get("market_context")
        if not isinstance(context, dict):
            return None
        sizing = context.get("options_sizing")
        if sizing is None:
            sizing = {}
        if not isinstance(sizing, dict):
            return None
        try:
            filled = execution.get("filled_quantity")
            exited = execution.get("exited_quantity") or 0
            lot_size = sizing.get("lot_size") or 65
            if any(isinstance(value, bool) for value in (filled, exited, lot_size)):
                return None
            filled, exited, lot_size = int(filled), int(exited), int(lot_size)
            if filled <= 0 or exited < 0 or exited > filled or lot_size <= 0:
                return None
            contract = option.get("contract") or {}
            if not isinstance(contract, dict):
                return None
            strike = int(contract.get("strike"))
            entry_price = float(execution.get("fill_price"))
            entry_limit = float(sizing.get("entry_limit") or option.get("original_option_entry") or entry_price)
            catastrophic = float(option.get("original_option_sl") or sizing.get("option_sl") or entry_price)
            target_1 = float(sizing.get("target_1") or sizing.get("option_target") or entry_price)
            spot_entry = float(execution.get("spot_at_fill") or trade.get("entry_price"))
            spot_sl = float(sizing.get("synthetic_spot_sl") or trade.get("stop_loss") or spot_entry)
            spot_t1 = float(trade.get("target_1") or spot_entry)
            spot_t2 = float(trade.get("target_2") or spot_t1)
            raw_greeks = sizing.get("greeks")
            greeks = OptionGreeks(**raw_greeks) if isinstance(raw_greeks, dict) else None
            bracket = OptionOrderBracket(
                security_id=str(contract.get("security_id") or ""),
                expiry_date=str(contract.get("expiry") or ""),
                strike=strike,
                option_type=str(contract.get("option_type") or "").upper(),
                entry_limit=entry_limit,
                synthetic_spot_sl=spot_sl,
                catastrophic_opt_sl=catastrophic,
                target_1=target_1,
                target_2=(float(sizing["target_2"]) if sizing.get("target_2") is not None else None),
                lots=max(1, int(sizing.get("lots") or sizing.get("final_lots") or
                                 math.ceil(filled / lot_size))),
                greeks=greeks,
                is_valid=True,
                lot_size=lot_size,
                signal_id=str(trade.get("signal_uuid") or trade.get("signal_id") or ""),
                spot_entry=spot_entry,
                spot_target_1=spot_t1,
                spot_target_2=spot_t2,
            )
            status = str(execution.get("status") or "")
            remaining = filled - exited
            record = OrderRecord(
                correlation_id=correlation_id,
                bracket=bracket,
                mode="PAPER",
                status=status,
                filled_quantity=filled,
                fill_price=entry_price,
                entry_spot=spot_entry,
                protection_confirmed=True,
                exited_quantity=exited,
                realized_pnl=float(execution.get("realized_pnl") or 0),
                active_spot_sl=float(execution.get("active_spot_sl") or spot_sl),
                t1_hit=execution.get("t1_hit") is True,
                atr_last_bar=str(execution.get("atr_last_bar") or ""),
                exit_reason=str(execution.get("exit_reason") or ""),
                pending_exit_quantity=(int(execution.get("pending_exit_quantity") or remaining)
                                       if status == "EXIT_PENDING" else 0),
            )
        except (TypeError, ValueError, OverflowError):
            return None
        return record if SignalDispatcher._valid_recovered_record(record) else None

    @staticmethod
    def _valid_recovered_record(record):
        return bool(
            record.mode == "PAPER"
            and record.status in {"FILLED", "PARTIALLY_FILLED", "EXIT_PENDING", "CLOSED"}
            and type(record.filled_quantity) is int and record.filled_quantity > 0
            and record.protection_confirmed is True
            and type(record.exited_quantity) is int
            and 0 <= record.exited_quantity <= record.filled_quantity
            and (record.status != "CLOSED" or record.exited_quantity == record.filled_quantity)
            and (record.status != "EXIT_PENDING" or
                 (type(record.pending_exit_quantity) is int
                  and 0 < record.pending_exit_quantity <= record.filled_quantity - record.exited_quantity))
        )

    def _publish(self, record):
        signal, _ = self._signals[record.correlation_id]
        previous = signal.market_context.get("execution", {})
        execution = {
            **previous,
            "mode": record.mode,
            "correlation_id": record.correlation_id,
            "status": record.status,
            "filled_quantity": record.filled_quantity,
            "fill_price": record.fill_price,
            "protection_confirmed": record.protection_confirmed,
            "exited_quantity": record.exited_quantity,
            "realized_pnl": record.realized_pnl,
            "exit_reason": record.exit_reason,
            "t1_hit": record.t1_hit,
            "active_spot_sl": record.active_spot_sl,
            "atr_last_bar": record.atr_last_bar,
            "pending_exit_quantity": record.pending_exit_quantity,
            "order_record": asdict(record),
        }
        signal.market_context["execution"] = execution
        for trade in self.positions.active_trades:
            if str(trade.get("signal_uuid")) == str(signal.id):
                trade.setdefault("market_context", {})["execution"] = execution
        return execution

    async def _sync(self, record):
        signal, atm = self._signals[record.correlation_id]
        execution = signal.market_context["execution"]
        # A protected fill is an auditable trade even if it has already exited.
        # Retry from both quote and spot paths without losing the original fill.
        if record.ready_to_register and not signal.trade_id:
            trade_id, status = await self.positions.add_trade(
                signal, execution["spot_at_fill"], atm=atm,
            )
            signal.trade_id = trade_id or None
            signal.trade_binding_status = status
        elif not signal.trade_id:
            signal.trade_binding_status = record.status
        sync = getattr(self.positions, "sync_execution", None)
        persisted = False
        if signal.trade_id:
            persisted = sync is None or await sync(signal, execution) is not False
        terminal = record.status in {"CANCELLED", "REJECTED", "CLOSED"}
        if terminal and (record.filled_quantity == 0 or persisted):
            self._signals.pop(record.correlation_id, None)
        return persisted

    async def _observe(self, record, full_chain, spot):
        bracket = record.bracket
        side = bracket.option_type.lower()
        row = next((row for row in full_chain
                    if str(row.get(f"{side}_security_id")) == bracket.security_id
                    and row.get("expiry_date") == bracket.expiry_date
                    and row.get("strike") == bracket.strike), None)
        had_fill = record.filled_quantity > 0
        quote_spot = row.get("underlying_spot") if row is not None else None
        valid_quote_spot = (isinstance(quote_spot, (int, float))
                            and not isinstance(quote_spot, bool)
                            and math.isfinite(quote_spot) and quote_spot > 0)
        if not valid_quote_spot:
            self.orders.on_spot(record.correlation_id, spot)
        else:
            self.orders.observe_quote(record.correlation_id, row.get(f"{side}_ask"),
                                      spot=quote_spot, bid=row.get(f"{side}_bid"))
        execution = self._publish(record)
        if not had_fill and record.filled_quantity > 0:
            execution["filled_at"] = datetime.now(timezone.utc).isoformat()
            execution["spot_at_fill"] = record.entry_spot
        await self._sync(record)
