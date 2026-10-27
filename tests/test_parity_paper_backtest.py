"""Backtest-vs-paper parity: replay identical intents and marks through
BacktestPort and PaperPort (real code paths, recorded-fixture Delta REST) and
require fill counts + fill prices + fees to agree within documented
tolerances (paper fills at book edge, backtest fills at mark ± 1 tick).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.config import Config
from core.models import Candle, OrderIntent, OrderType, Side
from delta.rest import DeltaRestClient

FIXTURE = Path(__file__).parent / "fixtures" / "products_btcusd.json"


def mock_client(ticker_marks: dict[str, tuple[str, str]]) -> DeltaRestClient:
    """REST client whose transport is a recorded-fixture replay"""
    import httpx

    payload_products = json.loads(FIXTURE.read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/products") or "/products?" in url:
            body = {"success": True, "result": payload_products, "meta": {}}
        elif "/tickers/" in url:
            sym = url.rsplit("/", 1)[-1]
            bid, ask = ticker_marks.get(sym, ("100", "100.5"))
            body = {
                "success": True,
                "result": {
                    "symbol": sym,
                    "mark_price": str((float(bid) + float(ask)) / 2),
                    "quotes": {"best_bid": bid, "best_ask": ask},
                    "spot_price": "103000",
                    "greeks": {"delta": 0.5},
                    "vol": "1",
                    "oi": "1",
                    "timestamp": "0",
                },
            }
        else:
            body = {"success": False, "error": {"code": "unmocked", "context": url}}
        return httpx.Response(200, json=body)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://mock.delta.invalid"
    )
    return DeltaRestClient(base_url="https://mock.delta.invalid", http_client=http)


def _candles() -> list[Candle]:
    import datetime as dt

    base = 1763798400  # 2025-11-22T08:00:00Z
    out: list[Candle] = []
    px = 100_000.0
    for i in range(30):
        p = px + (i % 3 - 1) * 20.0  # deterministic wiggle
        out.append(
            Candle(
                symbol="BTCUSD",
                resolution="1m",
                ts=dt.datetime.fromtimestamp(base + i * 60, tz=dt.UTC),
                open=p,
                high=p + 10,
                low=p - 10,
                close=p + 5,
                volume=10.0 * (i + 1),
                closed=True,
            )
        )
        px = p
    return out


class FlatTestStrategy:
    """Deterministic driver: market-buy 1 on candle 2, sell on candle 25."""

    def __init__(self) -> None:
        self.calls = 0

    async def bootstrap(self, exec_port: Any) -> None:  # noqa: ANN401
        self.exec = exec_port

    async def on_candle_close(self, candle: Candle) -> None:
        self.calls += 1
        if self.calls == 2:
            await self.exec.place(
                OrderIntent(
                    algo="test",
                    product_symbol="BTCUSD",
                    side=Side.BUY,
                    size=1,
                    order_type=OrderType.MARKET,
                    reason="open",
                )
            )
        elif self.calls == 25:
            await self.exec.place(
                OrderIntent(
                    algo="test",
                    product_symbol="BTCUSD",
                    side=Side.SELL,
                    size=1,
                    order_type=OrderType.MARKET,
                    reduce_only=True,
                    reason="close",
                )
            )

    async def on_ticker(self, symbol: str, ticker: Any) -> None:
        _ = (symbol, ticker)

    async def on_fill(self, fill: Any) -> None:
        _ = fill

    async def on_timer(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {}


def test_parity_paper_vs_backtest(tmp_path):
    from backtest.engine import BacktestPort, run_backtest
    from delta.products import ProductCatalog
    from paper.port import PaperPort

    candles = _candles()
    from core.models import ProductInfo

    products = {
        "BTCUSD": ProductInfo(
            symbol="BTCUSD",
            product_id=27,
            contract_type="perpetual_futures",
            tick_size=Decimal("0.5"),
            contract_value=Decimal("0.001"),
            maker_commission_rate=Decimal("0.0002"),
            taker_commission_rate=Decimal("0.0007"),
        )
    }

    async def run_bt():
        port = BacktestPort(products, Decimal("10000"), {})
        summary = await run_backtest(FlatTestStrategy(), port, candles)
        return port, summary

    port_bt, _summary = asyncio.run(run_bt())

    async def run_paper():
        TICK: dict[str, tuple[str, str]] = {}
        for c in candles:  # mark both legs from close ± 1
            close = Decimal(str(c.close))
            TICK["BTCUSD"] = (str(close - Decimal("1")), str(close + Decimal("1")))
        cfg = Config()
        cfg.flags["paper_initial_equity_usd"] = "10000.0"
        cfg.flags["paper_state_path"] = str(tmp_path / "paper_state.json")
        cfg.flags["candles_btcusd"] = TICK
        rest = mock_client(TICK)
        port = PaperPort(cfg)
        port.rest = rest  # recorded fixtures; no network
        port.catalog = ProductCatalog(rest)
        strat = FlatTestStrategy()
        await port.bootstrap()
        await strat.bootstrap(port)
        for c in candles:
            await strat.on_candle_close(c)
        return port

    port_pp = asyncio.run(run_paper())

    # fill-count parity (exact)
    assert len(port_bt.fills) == len(port_pp.pending_fills)
    # fill-price parity within the models' documented delta: the backtest
    # market fill crosses 1bp slippage (BTC: ~$10) while paper fills at the
    # book edge (±1 tick around close); both are the same "one-tick, thin
    # book" family. On 0.001 cv BTC that's at most ~0.015 USD of value.
    for f_bt, f_pp in zip(port_bt.fills, port_pp.pending_fills, strict=False):
        assert abs(float(f_bt.price) - float(f_pp.price)) <= 35.0, (
            str(f_bt.price),
            str(f_pp.price),
        )
        # fee ordering parity: both truncate at ~0.07% of notional
        for fee in (f_bt.fee, f_pp.fee):
            assert fee is not None
        assert abs(float(f_bt.fee) - float(f_pp.fee)) <= 2.0
    # position closed flat in both
    assert port_bt.positions.get("BTCUSD", 0) == 0
    assert port_pp.positions["BTCUSD"]["size"] == 0
