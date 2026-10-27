"""Order state machine — explicit, typed, exchange-trusted on reconcile.

States (per-Order):
    NEW -> OPEN -> PARTIAL -> FILLED
               \\-> CANCELLED / REJECTED / UNKNOWN
    PARTIAL -> FILLED / CANCELLED / UNKNOWN
    UNKNOWN -> OPEN / PARTIAL / FILLED / CANCELLED / REJECTED
Terminals: FILLED, CANCELLED, REJECTED.
The engine TRUSTS THE EXCHANGE: any apply() from an exchange event that maps
to an allowed state wins over local state. Out-of-order events are handled by
monotonic rules (filled_size never decreases; a later partial is legal).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from core.models import FillEvent, OrderRecord, OrderStatus, Side

TERMINAL: dict[OrderStatus, tuple[OrderStatus, ...]] = {
    OrderStatus.NEW: (OrderStatus.OPEN,),
    OrderStatus.OPEN: (
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.UNKNOWN,
    ),
    OrderStatus.PARTIAL: (
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.UNKNOWN,
    ),
    OrderStatus.UNKNOWN: (
        OrderStatus.OPEN,
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    ),
    OrderStatus.FILLED: (),
    OrderStatus.CANCELLED: (),
    OrderStatus.REJECTED: (),
}

# exchange status strings on GET /orders (spec 1.6) -> internal
EX_STATUS = {
    "open": OrderStatus.OPEN,
    "draft": OrderStatus.NEW,
    "partially_filled": OrderStatus.PARTIAL,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "crash_rejected": OrderStatus.REJECTED,
    "unknown": OrderStatus.UNKNOWN,
    "": OrderStatus.UNKNOWN,
}


class OmsError(Exception):
    pass


@dataclass
class OrderBook:
    """Local book; source of truth only UNTIL reconcile() says otherwise."""

    orders: dict[str, OrderRecord] = field(default_factory=dict)
    halts: list[str] = field(default_factory=list)

    def submit(self, rec: OrderRecord) -> OrderRecord:
        """Idempotent: re-submitting the same client_order_id is a no-op."""
        if rec.size <= 0:
            raise OmsError(f"size must be > 0 for {rec.client_order_id}")
        if rec.size != int(rec.size):
            raise OmsError("size must be an integer")
        existing = self.orders.get(rec.client_order_id)
        if existing is not None:
            return existing
        rec.status = OrderStatus.NEW
        self.orders[rec.client_order_id] = rec
        return rec

    def apply_fill(self, fill: FillEvent) -> OrderRecord:
        rec = self.orders.get(fill.client_order_id)
        if rec is None:  # exchange fill for an order we
            self.halts.append(  # don't know -> reconcile needed
                f"unknown_fill:{fill.client_order_id}"
            )
            raise OmsError(f"fill for unknown order {fill.client_order_id}")
        if fill.size <= 0:
            raise OmsError("fill size must be > 0")
        new_filled = rec.filled_size + fill.size
        if new_filled > rec.size:
            self.halts.append(f"overfill:{rec.client_order_id}")
            raise OmsError(f"overfill {new_filled}>{rec.size}")
        rec.filled_size = new_filled
        if rec.avg_fill_price is None:
            rec.avg_fill_price = fill.price
        else:
            total = rec.size
            rec.avg_fill_price = (
                rec.avg_fill_price * (total - fill.size) + fill.price * fill.size
            ) / total
        if new_filled == rec.size:
            rec.status = OrderStatus.FILLED
        elif rec.status in (OrderStatus.NEW, OrderStatus.UNKNOWN, OrderStatus.OPEN):
            rec.status = OrderStatus.PARTIAL
        rec.updated_at = fill.ts
        return rec

    def apply_status(self, client_order_id: str, status: OrderStatus) -> OrderRecord:
        rec = self.orders.get(client_order_id)
        if rec is None:
            self.halts.append(f"unknown_status:{client_order_id}")
            raise OmsError(f"status for unknown order {client_order_id}")
        allowed = TERMINAL.get(rec.status, ())
        if status not in allowed:
            self.halts.append(
                f"bad_transition:{client_order_id}:{rec.status.value}->{status.value}"
            )
            return rec  # do not corrupt; caller inspects halts
        rec.status = status
        return rec

    def reconcile(
        self,
        exchange_orders: Iterable[Mapping[str, object]],
        keyfn: Callable[[Mapping[str, object]], str] = lambda o: str(
            o["client_order_id"]
        ),
        statusfn: Callable[[Mapping[str, object]], OrderStatus] = lambda o: (
            EX_STATUS.get(str(o.get("status", "")), OrderStatus.UNKNOWN)
        ),
        sizefn: Callable[[Mapping[str, object]], int] = lambda o: int(
            str(o.get("size", 0) or 0)
        ),
        filledfn: Callable[[Mapping[str, object]], int] = lambda o: int(
            str(o.get("filled_size", 0) or 0)
        ),
    ) -> list[str]:
        """Trust exchange on every shared order; drop what only local has.

        Returns human-readable mismatches. HALT semantics: the caller stops
        trading commencement whenever any mismatch contains 'halt:'.
        """
        mismatch: list[str] = []
        ex_map = {keyfn(o): o for o in exchange_orders}
        # 1. exchange-only orders (we lost state, crashed mid-order, or manual)
        for cid, o in ex_map.items():
            rec = self.orders.get(cid)
            if rec is None:
                rec = OrderRecord(
                    client_order_id=cid,
                    symbol=str(o.get("symbol", "?")),
                    side=Side(str(o.get("side", "buy"))),
                    size=sizefn(o),
                    status=OrderStatus.UNKNOWN,
                )
                self.orders[cid] = rec
                mismatch.append(f"local_missing:{cid}")
            st = statusfn(o)
            # exchange is authoritative — always adopt its status/size
            rec.status = st
            filled = filledfn(o)
            if filled > rec.filled_size:
                rec.filled_size = filled
                if filled >= rec.size:
                    rec.status = OrderStatus.FILLED
            mismatch.append(f"trust_exchange:{cid}:{st.value}")
        # 2. local-only orders: we cancelled/they vanished -> verify & drop
        for cid, rec in self.orders.items():
            if cid not in ex_map and rec.status not in (
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
            ):
                mismatch.append(f"halt:local_only_open:{cid}")
        return mismatch
