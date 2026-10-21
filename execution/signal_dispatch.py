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
        """Persist completed-candle runner stops before waiting for option quotes."""
        for correlation_id in tuple(self._signals):
            try:
                record = self.orders.on_candles(correlation_id, candles)
                self._publish(record)
                await self._sync(record)
            except Exception:
                logger.exception("Paper candle update failed for %s; will retry", correlation_id)

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
