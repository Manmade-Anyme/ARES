"""Observed-quote paper execution and read-only recovery of Dhan super orders.

The live adapter is intentionally not enabled by main. Ambiguous submissions and
unconfirmed protection require reconciliation, never a blind submission retry.
"""
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import uuid
from zoneinfo import ZoneInfo

from models import OptionGreeks, OptionOrderBracket
from atr_trailing import completed_atr_bars


def _exchange_date():
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


@dataclass
class OrderRecord:
    correlation_id: str
    bracket: OptionOrderBracket
    mode: str = "PAPER"
    status: str = "SUBMITTED"
    filled_quantity: int = 0
    fill_price: float = 0.0
    entry_spot: float = 0.0
    protection_confirmed: bool = False
    exited_quantity: int = 0
    realized_pnl: float = 0.0
    active_spot_sl: float = 0.0
    t1_hit: bool = False
    atr_last_bar: str = ""
    exit_reason: str = ""
    pending_exit_quantity: int = 0
    slices: list = field(default_factory=list)
    rejection_reason: str = ""

    @property
    def ready_to_register(self):
        """Every observed protected fill needs an audit, including a completed loss."""
        return (self.filled_quantity > 0 and self.protection_confirmed
                and self.status in {"FILLED", "PARTIALLY_FILLED", "EXIT_PENDING", "CLOSED"})


class DhanSuperOrderAdapter:
    """Low-level Dhan transport, not a complete live execution runtime.

    Contract: https://dhanhq.co/docs/v2/super-order/
    A triggered exit is merely placed, so recovery never treats it as a fill.
    """

    # Super-order placement alone does not provide a safe synthetic exit loop.
    # Keep manager submission disabled until that complete runtime is implemented.

    def __init__(self, sdk):
        self.sdk = sdk

    def submit(self, bracket, quantity, target, tag):
        """Submit one atomic LIMIT entry with both contingent protective legs."""
        response = self.sdk.place_super_order(
            security_id=bracket.security_id, exchange_segment="NSE_FNO",
            transaction_type="BUY", quantity=quantity, order_type="LIMIT",
            product_type="INTRADAY", price=bracket.entry_limit,
            targetPrice=target, stopLossPrice=bracket.catastrophic_opt_sl,
            trailingJump=0.0, tag=tag,
        )
        if not isinstance(response, dict) or response.get("status") != "success":
            raise RuntimeError("Super-order submission not confirmed")
        data = response.get("data") or {}
        if not data.get("orderId"):
            raise RuntimeError("Super-order ID not confirmed")
        return str(data["orderId"])

    def order_book(self):
        """Read broker state; failed or malformed reads cannot confirm protection."""
        response = self.sdk.get_super_order_list()
        if not isinstance(response, dict) or response.get("status") != "success" or not isinstance(response.get("data"), list):
            raise RuntimeError("Super-order reconciliation unavailable")
        return response["data"]


class OrderManager:
    """Track bounded entries, whole-lot paper exits, and durable live intents.

    The journal must have one writer. Live synthetic stop handling is deliberately
    fail-closed (RECONCILIATION_REQUIRED); this adapter is not a live trading loop.
    """

    def __init__(self, broker=None, journal_path=None, allow_live=False):
        if broker is not None and (not allow_live or journal_path is None):
            raise ValueError("Live execution requires explicit opt-in and a durable journal")
        self.broker = broker
        self.journal_path = Path(journal_path) if journal_path is not None else None
        self.records = {}
        if self.journal_path and self.journal_path.exists():
            for raw in json.loads(self.journal_path.read_text()):
                values = raw.pop("bracket")
                if values.get("greeks") is not None:
                    values["greeks"] = OptionGreeks(**values["greeks"])
                record = OrderRecord(bracket=OptionOrderBracket(**values), **raw)
                expected_mode = "LIVE" if broker else "PAPER"
                if record.mode != expected_mode:
                    raise ValueError("Journal execution mode does not match manager")
                if broker and record.status not in {"CANCELLED", "REJECTED", "CLOSED"}:
                    record.status = "RECONCILIATION_REQUIRED"
                    record.protection_confirmed = False
                self.records[record.correlation_id] = record

    def _save(self):
        if self.journal_path is None:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.journal_path.parent, prefix=".orders-")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump([asdict(r) for r in self.records.values()], handle, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.journal_path)
            directory = os.open(self.journal_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _valid(bracket):
        try:
            expiry_valid = date.fromisoformat(bracket.expiry_date) >= _exchange_date()
        except (TypeError, ValueError):
            return False
        prices = (bracket.catastrophic_opt_sl, bracket.entry_limit, bracket.target_1)
        valid = (bracket.is_valid and expiry_valid and bool(bracket.security_id)
                 and bracket.option_type in {"CE", "PE"}
                 and type(bracket.lots) is int and bracket.lots > 0
                 and type(bracket.lot_size) is int and bracket.lot_size > 0
                 and all(_positive(p) for p in prices)
                 and prices[0] < prices[1] < prices[2]
                 and _positive(bracket.synthetic_spot_sl) and _positive(bracket.spot_entry)
                 and _positive(bracket.spot_target_1))
        if bracket.lots > 1:
            valid = valid and _positive(bracket.target_2) and bracket.target_2 > bracket.target_1 and _positive(bracket.spot_target_2)
        direction = 1 if bracket.option_type == "CE" else -1
        return bool(valid and direction * (bracket.spot_entry - bracket.synthetic_spot_sl) > 0
                    and direction * (bracket.spot_target_1 - bracket.spot_entry) > 0
                    and (bracket.lots == 1 or direction * (bracket.spot_target_2 - bracket.spot_target_1) > 0))

    def dispatch_bracket(self, bracket, correlation_id=None):
        """Register a paper intent; live runtime remains blocked before any broker call."""
        identity = correlation_id or bracket.signal_id
        key = hashlib.sha256(str(identity).encode()).hexdigest()[:24] if identity else uuid.uuid4().hex[:24]
        if key in self.records:
            existing = self.records[key]
            if existing.bracket != bracket:
                raise ValueError("Correlation ID already belongs to a different bracket")
            return existing
        record = OrderRecord(key, bracket, mode="LIVE" if self.broker else "PAPER",
                             active_spot_sl=bracket.synthetic_spot_sl)
        self.records[key] = record
        if self.broker is not None:
            record.status = "REJECTED"
            record.rejection_reason = "LIVE_RUNTIME_UNSUPPORTED"
            self._save()
            return record
        if not self._valid(bracket):
            record.status = "REJECTED"
            self._save()
            return record
        self._save()
        return record

    def confirm_fill(self, correlation_id, quantity, price, protection_confirmed):
        """Apply cumulative fill evidence, rejecting overfills and limit violations."""
        if self.broker is not None:
            raise ValueError("Live fills must come from broker reconciliation")
        return self._confirm_fill(correlation_id, quantity, price, protection_confirmed)

    def _confirm_fill(self, correlation_id, quantity, price, protection_confirmed):
        record = self.records[correlation_id]
        if type(quantity) is not int or not record.filled_quantity <= quantity <= record.bracket.lots * record.bracket.lot_size:
            raise ValueError("Invalid cumulative fill quantity")
        if not _positive(price) or price > record.bracket.entry_limit:
            raise ValueError("Invalid LIMIT fill price")
        if record.status in {"CANCELLED", "REJECTED", "CLOSED"}:
            raise ValueError("Cannot fill terminal order")
        record.filled_quantity = quantity
        record.fill_price = price
        record.protection_confirmed = protection_confirmed is True
        record.status = "FILLED" if quantity == record.bracket.lots * record.bracket.lot_size else "PARTIALLY_FILLED"
        self._save()
        return record

    def observe_quote(self, correlation_id, ask, spot=None, bid=None):
        """Paper fills use observed executable asks; exits require observed bids."""
        record = self.records[correlation_id]
        if record.mode != "PAPER":
            return record
        if record.status == "SUBMITTED" and date.fromisoformat(record.bracket.expiry_date) < _exchange_date():
            record.status = "CANCELLED"
            self._save()
        crossed = _positive(ask) and _positive(bid) and bid > ask
        if spot is not None:
            self.on_spot(correlation_id, spot, option_bid=None if crossed else bid)
        if crossed:
            return record
        if record.status == "SUBMITTED" and _positive(ask) and ask <= record.bracket.entry_limit:
            record.entry_spot = spot if _positive(spot) else record.bracket.spot_entry
            self.confirm_fill(correlation_id, record.bracket.lots * record.bracket.lot_size, ask, True)
        if record.status == "EXIT_PENDING" and _positive(bid):
            self._paper_exit(record, record.pending_exit_quantity, bid, record.exit_reason)
        elif record.filled_quantity > record.exited_quantity and _positive(bid) and bid <= record.bracket.catastrophic_opt_sl:
            self._paper_exit(record, record.filled_quantity - record.exited_quantity, bid, "SL_HIT")
        elif record.filled_quantity > record.exited_quantity and _positive(bid):
            b = record.bracket
            if not record.t1_hit and bid >= b.target_1:
                lots = record.filled_quantity // b.lot_size
                self._paper_exit(record, max(1, lots // 2) * b.lot_size, bid, "T1_HIT")
        return record

    def _paper_exit(self, record, quantity, bid, reason):
        quantity = min(quantity, record.filled_quantity - record.exited_quantity)
        if quantity <= 0:
            return
        record.exit_reason = reason
        if not _positive(bid):
            record.pending_exit_quantity = quantity
            record.status = "EXIT_PENDING"
        else:
            record.realized_pnl += quantity * (bid - record.fill_price)
            record.exited_quantity += quantity
            record.pending_exit_quantity = 0
            record.status = "CLOSED" if record.exited_quantity == record.filled_quantity else "FILLED"
            if reason == "T1_HIT":
                record.t1_hit = True
                record.active_spot_sl = record.entry_spot or record.bracket.spot_entry
                record.atr_last_bar = datetime.now(timezone.utc).isoformat()
        self._save()

    def on_spot(self, correlation_id, spot, option_bid=None):
        """Invalidate pending entries and trigger directional synthetic paper exits."""
        record = self.records[correlation_id]
        if not _positive(spot) or record.status in {"CANCELLED", "REJECTED", "CLOSED"}:
            return record
        b = record.bracket
        direction = 1 if b.option_type == "CE" else -1
        stop_hit = direction * (spot - record.active_spot_sl) <= 0
        t1_hit = direction * (spot - b.spot_target_1) >= 0
        if not record.filled_quantity:
            if stop_hit or t1_hit or date.fromisoformat(b.expiry_date) < _exchange_date():
                record.status = "CANCELLED" if record.mode == "PAPER" else "RECONCILIATION_REQUIRED"
                self._save()
            return record
        if record.mode != "PAPER":
            if stop_hit:
                record.status = "RECONCILIATION_REQUIRED"
                record.protection_confirmed = False
                self._save()
            return record
        remaining = record.filled_quantity - record.exited_quantity
        if record.status == "EXIT_PENDING":
            if _positive(option_bid):
                self._paper_exit(record, record.pending_exit_quantity, option_bid, record.exit_reason)
        elif not record.t1_hit and stop_hit:
            self._paper_exit(record, remaining, option_bid, "SL_HIT")
        elif not record.t1_hit and t1_hit:
            lots = record.filled_quantity // b.lot_size
            quantity = min(remaining, max(1, lots // 2) * b.lot_size)
            self._paper_exit(record, quantity, option_bid, "T1_HIT")
        return record

    def on_candles(self, correlation_id, candles):
        """Trail paper runners on completed five-minute closes, never fabricate bids."""
        record = self.records[correlation_id]
        if record.mode != "PAPER" or not record.t1_hit or record.status != "FILLED":
            return record
        # Old journals have no T1 timestamp; begin watching now, without replaying exits.
        if not record.atr_last_bar:
            record.atr_last_bar = datetime.now(timezone.utc).isoformat()
            self._save()
        direction = 1 if record.bracket.option_type == "CE" else -1
        for bar in completed_atr_bars(candles):
            if datetime.fromisoformat(bar["timestamp"]) <= datetime.fromisoformat(record.atr_last_bar):
                continue
            record.atr_last_bar = bar["timestamp"]
            if direction * (bar["close"] - record.active_spot_sl) < 0:
                self._paper_exit(record, record.filled_quantity - record.exited_quantity,
                                 None, "ATR_SL_HIT")
                break
            candidate = bar["bullish_sl" if direction == 1 else "bearish_sl"]
            if candidate is not None:
                bound = max if direction == 1 else min
                record.active_spot_sl = round(bound(
                    record.entry_spot or record.bracket.spot_entry,
                    record.active_spot_sl, candidate,
                ), 2)
            self._save()
        return record

    def reconcile(self):
        """Reconcile cumulative fills and coverage; triggered legs are not fills."""
        if self.broker is None:
            return
        for record in self.records.values():
            if record.status not in {"CANCELLED", "REJECTED", "CLOSED"}:
                record.protection_confirmed = False
                record.status = "RECONCILIATION_REQUIRED"
        self._save()
        book = self.broker.order_book()
        for record in self.records.values():
            if record.status in {"CANCELLED", "REJECTED", "CLOSED"}:
                continue
            quantity, cost, protected = 0, 0.0, True
            for part in record.slices:
                matches = [row for row in book if (part["order_id"] is not None and str(row.get("orderId")) == part["order_id"]) or row.get("correlationId") == part["tag"]]
                if len(matches) != 1:
                    protected = False
                    continue
                row = matches[0]
                part["order_id"] = str(row["orderId"])
                filled = row.get("filledQty", 0)
                price = row.get("averageTradedPrice", 0)
                if type(filled) is not int or not 0 <= filled <= part["quantity"] or (filled and (not _positive(price) or price > record.bracket.entry_limit)):
                    protected = False
                    continue
                quantity += filled
                cost += filled * price if filled else 0.0
                legs = row.get("legDetails") or []
                for name in ("TARGET_LEG", "STOP_LOSS_LEG"):
                    coverage = sum(leg.get("remainingQuantity", 0) for leg in legs
                                   if leg.get("legName") == name and leg.get("orderStatus") in {"PENDING", "OPEN"}
                                   and type(leg.get("remainingQuantity")) is int)
                    protected = protected and coverage >= filled
                if row.get("orderStatus") in {"CLOSED", "REJECTED", "CANCELLED"}:
                    protected = False
            if quantity and quantity >= record.filled_quantity:
                self._confirm_fill(record.correlation_id, quantity, cost / quantity, protected)
            if not protected:
                record.status = "RECONCILIATION_REQUIRED"
            elif not quantity:
                record.status = "SUBMITTED"
            self._save()
