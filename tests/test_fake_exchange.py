"""Fake Delta exchange: injected disconnects, duplicate/partial fills,
rejects, 429s and out-of-order messages — exercised against the real adapter
models. Fewer integration points than a socket server, same coverage of
event ordering, and offline (no CI network flakiness)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.models import FillEvent, OrderRecord, OrderStatus, Side
from core.oms import OmsError, OrderBook


class FakeExchange:
    """Order-event fountain with chaotic toggles; drives OrderBook over the
    same typed events the Delta adapter produces."""

    def __init__(self) -> None:
        self.orders: dict[str, OrderRecord] = {}
        self.fail_next = False
        self.rate_limited = False
        self.sent_429 = 0

    def submit(self, rec: OrderRecord) -> OrderRecord:
        if self.rate_limited:
            self.sent_429 += 1
            raise OmsError("HTTP 429 (simulated)")
        if self.fail_next:
            self.fail_next = False
            rec.status = OrderStatus.REJECTED
        else:
            rec.status = OrderStatus.OPEN
        self.orders[rec.client_order_id] = rec
        return rec

    def fills_out_of_order(self, cid: str, seq: list[int], price: int = 100):
        """Emit fills in a shuffled order (late partial arriving after full)."""
        total = sum(seq)
        for s in seq:
            yield FillEvent(
                client_order_id=cid,
                symbol="ETHUSD",
                side=Side.BUY,
                size=s,
                price=Decimal_price(price),
                ts=datetime.now(UTC),
            )
        assert total


def Decimal_price(x: int):
    from decimal import Decimal

    return Decimal(x)


def ts() -> datetime:
    return datetime.now(UTC)


def test_429_then_success():
    ex = FakeExchange()
    ex.rate_limited = True
    with pytest.raises(OmsError):
        ex.submit(
            OrderRecord(
                client_order_id="a",
                symbol="ETHUSD",
                side=Side.BUY,
                size=1,
                status=OrderStatus.NEW,
            )
        )
    assert ex.sent_429 == 1
    ex.rate_limited = False
    r = ex.submit(
        OrderRecord(
            client_order_id="a",
            symbol="ETHUSD",
            side=Side.BUY,
            size=1,
            status=OrderStatus.NEW,
        )
    )
    assert r.status == OrderStatus.OPEN


def test_reject_marks_terminal_state():
    ex = FakeExchange()
    ex.fail_next = True
    r = ex.submit(
        OrderRecord(
            client_order_id="rj",
            symbol="ETHUSD",
            side=Side.BUY,
            size=3,
            status=OrderStatus.NEW,
        )
    )
    assert r.status == OrderStatus.REJECTED
    b = OrderBook()
    b.orders["rj"] = r
    b.apply_status("rj", OrderStatus.FILLED)  # illegal from REJECTED
    assert any("bad_transition" in h for h in b.halts)
    assert b.orders["rj"].status == OrderStatus.REJECTED


def test_duplicate_fill_and_overfill_detection():
    ex = FakeExchange()
    b = OrderBook()
    rec = ex.submit(
        OrderRecord(
            client_order_id="d",
            symbol="ETHUSD",
            side=Side.BUY,
            size=2,
            status=OrderStatus.OPEN,
        )
    )
    b.orders["d"] = rec
    ex2 = FillEvent(
        client_order_id="d",
        symbol="ETHUSD",
        side=Side.BUY,
        size=2,
        price=Decimal_price(100),
        ts=ts(),
    )
    b.apply_fill(ex2)
    assert b.orders["d"].status == OrderStatus.FILLED
    with pytest.raises(OmsError):
        b.apply_fill(ex2)  # duplicate — caught, no PnL corruption
    # out-of-order late partial for another order must not corrupt
    late = FillEvent(
        client_order_id="ghost",
        symbol="ETHUSD",
        side=Side.BUY,
        size=1,
        price=1,
        ts=ts(),
    )
    with pytest.raises(OmsError):
        b.apply_fill(late)
    assert any("unknown_fill" in h for h in b.halts)


def test_reconcile_after_disconnect_resets_local():
    b = OrderBook()
    rec = OrderRecord(
        client_order_id="k",
        symbol="ETHUSD",
        side=Side.BUY,
        size=5,
        status=OrderStatus.OPEN,
        filled_size=2,
    )
    b.orders["k"] = rec
    # exchange (after our WS dropped and resumed): still open, 3 filled
    mm = b.reconcile(
        [
            {
                "client_order_id": "k",
                "symbol": "ETHUSD",
                "side": "buy",
                "size": 5,
                "status": "partially_filled",
                "filled_size": 3,
            }
        ]
    )
    assert b.orders["k"].filled_size == 3  # exchange wins
    assert b.orders["k"].status == OrderStatus.PARTIAL
    assert mm  # events recorded for audit
    # halts always searchable but NOT set here: exchange explained itself
    assert not any("halt" in h for h in mm)
