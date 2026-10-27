"""Unit tests: OMS order state machine + reconciliation (exchange trusted)."""

from datetime import UTC, datetime

import pytest

from core.models import FillEvent, OrderRecord, OrderStatus, Side
from core.oms import OmsError, OrderBook


def now0():
    return datetime(2026, 10, 22, tzinfo=UTC)


def rec(cid="o1", size=10):
    return OrderRecord(
        client_order_id=cid,
        symbol="ETHUSD",
        side=Side.BUY,
        size=size,
        status=OrderStatus.NEW,
    )


def fill(cid, size, price):
    return FillEvent(
        client_order_id=cid,
        symbol="ETHUSD",
        side=Side.BUY,
        size=size,
        price=price,
        ts=now0(),
    )


def test_idempotent_submit_and_negative_size():
    b = OrderBook()
    r = b.submit(rec())
    r2 = b.submit(rec())
    assert r is r2
    assert len(b.orders) == 1
    with pytest.raises(OmsError):
        b.submit(rec(cid="o2", size=0))
    with pytest.raises(OmsError):
        b.submit(rec(cid="o3", size=-1))


def test_full_lifecycle_transitions():
    b = OrderBook()
    b.submit(rec())
    b.reconcile(
        [
            {
                "client_order_id": "o1",
                "symbol": "ETHUSD",
                "side": "buy",
                "size": 10,
                "status": "open",
                "filled_size": 0,
            }
        ]
    )
    assert b.orders["o1"].status == OrderStatus.OPEN
    b.apply_fill(fill("o1", 4, 4000))
    assert b.orders["o1"].status == OrderStatus.PARTIAL
    assert b.orders["o1"].filled_size == 4
    b.apply_fill(fill("o1", 6, 4000))
    assert b.orders["o1"].status == OrderStatus.FILLED
    with pytest.raises(OmsError):  # dead order
        b.apply_fill(fill("o1", 1, 4000))


def test_reconcile_trusts_exchange_and_flags_local_only():
    b = OrderBook()
    b.submit(rec("local_alive"))
    b.submit(rec("local_filled"))
    b.apply_status("local_filled", OrderStatus.FILLED)
    ex = [
        {
            "client_order_id": "local_alive",
            "symbol": "ETHUSD",
            "side": "buy",
            "size": 10,
            "status": "cancelled",
            "filled_size": 0,
        },
        {
            "client_order_id": "ghost",
            "symbol": "ETHUSD",
            "side": "buy",
            "size": 5,
            "status": "filled",
            "filled_size": 5,
        },
    ]
    mm = b.reconcile(ex)
    assert b.orders["local_alive"].status == OrderStatus.CANCELLED
    assert b.orders["ghost"].status == OrderStatus.FILLED
    assert any("local_missing:ghost" in m for m in mm)
    # an open local-only order that exchange forgot = HALT
    b2 = OrderBook()
    b2.submit(rec("orphan"))
    mm2 = b2.reconcile([])
    assert any("halt:local_only_open:orphan" in m for m in mm2)


def test_out_of_order_and_duplicate_fills_detected():
    b = OrderBook()
    b.submit(rec(size=5))
    b.apply_fill(fill("o1", 5, 100))  # fills complete
    late = fill("o1", 5, 100)  # duplicate late fill
    with pytest.raises(OmsError):
        b.apply_fill(late)
    assert b.orders["o1"].status.name == "FILLED"
    b2 = OrderBook()
    b2.submit(rec(cid="x", size=5))
    with pytest.raises(OmsError):
        b2.apply_fill(
            FillEvent(
                client_order_id="nope",
                symbol="ETHUSD",
                side=Side.BUY,
                size=1,
                price=1,
                ts=now0(),
            )
        )
    assert any("unknown_fill" in h for h in b2.halts)


def test_bad_transitions_recorded_not_applied():
    b = OrderBook()
    b.submit(rec())
    b.apply_fill(fill("o1", 10, 100))  # -> FILLED
    before = b.orders["o1"].status
    out = b.apply_status("o1", OrderStatus.OPEN)
    assert out.status == before
    assert any("bad_transition" in h for h in b.halts)
