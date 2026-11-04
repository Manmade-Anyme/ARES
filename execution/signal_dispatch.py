"""MANM-176: bridge validated paper orders to the existing spot analytics lifecycle.

This runtime deliberately does not enable a live adapter. Broker recovery/session
policy must be provisioned before live order state can own trade registration.
"""
from datetime import datetime, timezone
import logging
import math

from config import settings

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
                      or {})
        stop_domain = exit_state.get("stop_domain")
        index_break_even = (stop_domain == "SPOT_INDEX" and reason == "STOPPED_OUT_AT_BE"
                            and option.get("highest_milestone") == 1)
        initial_index_stop = (stop_domain == "SPOT_INDEX" and reason == "SL_HIT"
                              and option.get("highest_milestone") == 0)
        if (not trade_signal or option.get("policy_version") != "option_premium_protection_v2"
                or (stop_domain != "OPTION_PREMIUM" and not index_break_even and not initial_index_stop)
                or exit_state.get("reason") != reason):
            return False
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
            await self._sync(record)
            return True
        return False

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
