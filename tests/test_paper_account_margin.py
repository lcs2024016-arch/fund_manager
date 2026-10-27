"""Regression test (2026-10-27): PaperPort.account() must report
`margin_mode` in its raw payload, exactly like the live account does.

AllInOneStrategy only confirms cross-margin when
`(account.raw or {}).get("margin_mode")` is present, and
PortfolioManager.global_block_new() vetoes EVERY new lot with
`cross_margin_not_confirmed` while it is None — six paper sessions ran
with zero orders because of this.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from decimal import Decimal

from core.config import load_config
from core.models import OrderStatus
from paper.port import PaperPort

FIXTURE = Path(__file__).parent / "fixtures" / "products_btcusd.json"


def _mocked_paper_port(tmp_path: Path) -> PaperPort:
    """PaperPort whose REST transport is a fixture replay (no network)."""
    import paper.port as pp

    payload_products = json.loads(FIXTURE.read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/products") or "/products?" in url:
            body = {"success": True, "result": payload_products, "meta": {}}
        elif "/tickers/" in url:
            body = {"success": True, "result": {
                "symbol": url.rsplit("/", 1)[-1],
                "mark_price": "100.25",
                "quotes": {"best_bid": "100", "best_ask": "100.5"},
                "spot_price": "100.25", "greeks": {"delta": 0.5},
                "vol": "1", "oi": "1", "timestamp": "0",
            }}
        else:
            body = {"success": True, "result": {"data": []}, "meta": {}}
        return httpx.Response(200, json=body)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://mock.delta.invalid")

    original = pp.DeltaRestClient

    class MockClient(original):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            kw.pop("http_client", None)
            super().__init__(base_url="https://mock.delta.invalid",
                             api_key="", api_secret="", http_client=http)

    pp.DeltaRestClient = MockClient
    try:
        cfg = load_config("config.yaml")
        cfg.flags["paper_state_path"] = str(tmp_path / "paper_state.json")
        return PaperPort(cfg)
    finally:
        pp.DeltaRestClient = original


def test_paper_account_reports_cross_margin(tmp_path: Path) -> None:
    port = _mocked_paper_port(tmp_path)

    async def run():
        await port.bootstrap()
        return await port.account()

    snap = asyncio.run(run())
    assert (snap.raw or {}).get("margin_mode") == "cross"


def _paper_port_with_book(tmp_path: Path, bid: str, ask: str) -> PaperPort:
    port = _mocked_paper_port(tmp_path)

    async def run():
        await port.bootstrap()
        # keep the original transport but serve a fixed two-sided book
        rest = port.rest
        orig_get_ticker = rest.get_ticker

        async def get_ticker(symbol):
            raw = await orig_get_ticker(symbol)
            if isinstance(raw, dict):
                quotes = raw.get("quotes") or {}
                quotes.update({"best_bid": bid, "best_ask": ask})
                raw["quotes"] = quotes
                raw["mark_price"] = str((float(bid) + float(ask)) / 2)
            return raw

        rest.get_ticker = get_ticker

    asyncio.run(run())
    return port


def _lot_tp_intent(buy: bool, limit: str, size: int = 10) -> "OrderIntent":
    from core.models import OrderIntent, OrderType, Side, TimeInForce

    return OrderIntent(
        algo="aio", product_symbol="BTCUSD", product_id=None,
        side=Side.BUY if buy else Side.SELL, size=size,
        order_type=OrderType.LIMIT, limit_price=Decimal(limit),
        time_in_force=TimeInForce.GTC, post_only=True, reduce_only=True,
        client_order_id="aio-btc-lottp-test", purpose="lot_tp",
    )


def test_reduce_only_limit_far_from_book_rests(tmp_path: Path) -> None:
    """Lot TP 1% below the book must REST, not instantly fill at the edge."""
    port = _paper_port_with_book(tmp_path, bid="100", ask="100.5")

    async def run():
        res = await port.place(_lot_tp_intent(buy=True, limit="99.0"))
        resting = await port.open_orders("BTCUSD")
        fills = list(port.pending_fills)
        # now the book trades down through the resting TP
        port.positions.clear()
        await port.ticker("BTCUSD")  # unchanged book: no fill
        n_before = len(port.pending_fills)
        rest = port.rest
        orig = rest.get_ticker

        async def crossed_ticker(symbol):
            raw = await orig(symbol)
            if isinstance(raw, dict):
                (raw.setdefault("quotes", {}) or {}).update(
                    {"best_bid": "98.5", "best_ask": "98.9"})
                raw["mark_price"] = "98.7"
            return raw

        rest.get_ticker = crossed_ticker
        await port.ticker("BTCUSD")
        return res, resting, fills, n_before, list(port.pending_fills)

    res, resting, fills, n_before, after = asyncio.run(run())
    assert res.accepted and res.status == OrderStatus.OPEN and res.filled_size == 0
    assert len(resting) == 1 and not fills
    assert n_before == 0
    assert len(after) == 1 and after[0].price == Decimal("99.0")
    assert after[0].reduce_only and after[0].client_order_id == "aio-btc-lottp-test"


def test_non_reduce_only_limit_still_fills_at_edge(tmp_path: Path) -> None:
    """Entries keep the instant-fill-at-book-edge parity behavior."""
    port = _paper_port_with_book(tmp_path, bid="100", ask="100.5")

    from core.models import OrderIntent, OrderType, Side, TimeInForce

    intent = OrderIntent(
        algo="aio", product_symbol="BTCUSD", product_id=None,
        side=Side.BUY, size=10, order_type=OrderType.LIMIT,
        limit_price=Decimal("100.3"), time_in_force=TimeInForce.GTC,
        post_only=True, reduce_only=False, client_order_id="aio-btc-lotentry-t",
        purpose="lot_entry",
    )

    async def run():
        return await port.place(intent)

    res = asyncio.run(run())
    assert res.accepted and res.status == OrderStatus.FILLED
    assert res.avg_fill_price == Decimal("100.5")  # filled at the ask edge


def test_cancel_removes_resting_order(tmp_path: Path) -> None:
    port = _paper_port_with_book(tmp_path, bid="100", ask="100.5")

    async def run():
        await port.place(_lot_tp_intent(buy=True, limit="99.0"))
        ok = await port.cancel("aio-btc-lottp-test", "BTCUSD")
        resting = await port.open_orders("BTCUSD")
        return ok, resting

    ok, resting = asyncio.run(run())
    assert ok and not resting
