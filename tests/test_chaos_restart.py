"""Chaos: kill the process mid-order, restart, reconcile; no duplicates."""

from core.models import OrderRecord, OrderStatus, Side
from core.oms import OrderBook

EXCHANGE_SEES = [
    {
        "client_order_id": "k1",
        "symbol": "ETHUSD",
        "side": "buy",
        "size": 5,
        "status": "partially_filled",
        "filled_size": 3,
    }
]


def restart_with_reconciliation(local_orders, exchange):
    """(chaos helper) a 'crashed' process loses local state; on restart it
    must rebuild the book from the EXCHANGE and never repost duplicates."""
    b = OrderBook()
    mismatches = b.reconcile(exchange)
    return b, mismatches


def test_crash_mid_order_reconciles_without_duplicates():
    b1 = OrderBook()
    b1.submit(
        OrderRecord(
            client_order_id="k1",
            symbol="ETHUSD",
            side=Side.BUY,
            size=5,
            status=OrderStatus.NEW,
        )
    )
    # ... process dies before the exchange ack arrives locally
    b2, mm = restart_with_reconciliation([], EXCHANGE_SEES)
    assert len(b2.orders) == 1  # one order, not two
    assert b2.orders["k1"].filled_size == 3
    assert b2.orders["k1"].status == OrderStatus.PARTIAL
    assert any("trust_exchange:k1" in m for m in mm)
    # resume: no repost (idempotent submit by client id)
    assert (
        b2.submit(
            OrderRecord(
                client_order_id="k1",
                symbol="ETHUSD",
                side=Side.BUY,
                size=5,
                status=OrderStatus.NEW,
            )
        )
        is b2.orders["k1"]
    )


def test_crash_with_local_only_open_order_halts():
    # exchange knows nothing of an order we think is resting → HALT flag
    b = OrderBook()
    b.submit(
        OrderRecord(
            client_order_id="lost",
            symbol="ETHUSD",
            side=Side.BUY,
            size=2,
            status=OrderStatus.OPEN,
        )
    )
    mm = b.reconcile([])
    assert any(m.startswith("halt:") for m in mm)
