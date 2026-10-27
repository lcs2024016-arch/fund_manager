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

from core.config import load_config
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
