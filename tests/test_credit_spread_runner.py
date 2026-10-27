"""Runner adapter smoke tests: option symbol parsing and the pre-trade
veto path through the runner's _send with a stub port."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from core.models import (
    AccountSnapshot,
    OrderIntent,
    OrderResult,
    Side,
    Ticker,
)
from strategy.credit_spread import (
    CreditSpreadRunner,
    Quote,
    SpreadCfg,
    _parse_option_symbol,
)
from strategy.credit_spread import CreditSpreadRunner as _R  # noqa: F401


def test_parse_option_symbol():
    got = _parse_option_symbol("C-BTC-79600-251026")
    assert got == ("BTC", "call", Decimal("79600"), "2026-10-25")
    got = _parse_option_symbol("P-ETH-3100-010126")
    assert got == ("ETH", "put", Decimal("3100"), "2026-01-01")
    assert _parse_option_symbol("BTCUSD") is None
    assert _parse_option_symbol("C-BTC-XX-251026") is None


class StubPort:
    """Paper-port-shaped stub for the veto path."""

    def __init__(self, accept: bool = True) -> None:
        self.accepted = accept
        self.placed: list[OrderIntent] = []
        self.products_db: dict[str, Any] = {}

    async def product(self, symbol: str):
        from core.models import ProductInfo

        return ProductInfo(
            symbol=symbol,
            product_id=1,
            contract_type="call_options",
            tick_size=Decimal("0.0001"),
            contract_value=Decimal("0.001"),
        )

    async def place(self, intent: OrderIntent):
        if not self.accepted:
            return None
        self.placed.append(intent)
        return OrderResult(client_order_id=intent.client_order_id, accepted=True)

    async def account(self):
        return AccountSnapshot(
            equity=Decimal("10000"),
            wallet_balance=Decimal("10000"),
            available_balance=Decimal("9000"),
            used_margin=Decimal("1000"),
        )

    async def position(self, symbol: str):
        return None

    async def open_orders(self):
        return []

    async def ticker(self, symbol: str):
        return Ticker(
            symbol=symbol, best_bid=Decimal("0.0500"), best_ask=Decimal("0.0502")
        )


def _quote(sym="C-BTC-79600-251026", delta=0.2, bid="0.0500", ask="0.0502"):
    return Quote(
        symbol=sym,
        underlying="BTC",
        kind="call",
        strike=Decimal(sym.split("-")[2]),
        expiry="2026-10-25",
        delta=delta,
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        bid_size=10,
        contract_value=Decimal("0.001"),
        underlying_price=Decimal("103000"),
    )


@pytest.mark.asyncio
async def test_send_applies_risk_veto_and_places():
    r = CreditSpreadRunner(SpreadCfg())
    port = StubPort()
    r.port = port
    acct = await port.account()
    intent = OrderIntent(
        algo="credit_spread",
        product_symbol="C-BTC-79600-251026",
        side=Side.SELL,
        size=10,
        purpose="spread_short",
    )
    r.quotes = {intent.product_symbol: _quote()}
    ok = await r._send(intent, acct, veto=True)  # noqa: SLF001
    assert ok and len(port.placed) == 1
    assert r.book.orders  # mirrored for idempotency


@pytest.mark.asyncio
async def test_send_veto_stops_oversized_order():
    r = CreditSpreadRunner(SpreadCfg())
    port = StubPort()
    r.port = port
    acct = await port.account()
    intent = OrderIntent(
        algo="credit_spread",
        product_symbol="C-BTC-79600-251026",
        side=Side.SELL,
        size=100000,
        purpose="spread_short",
    )
    r.quotes = {intent.product_symbol: _quote(delta=0.2)}
    ok = await r._send(intent, acct, veto=True)  # noqa: SLF001
    assert not ok
    assert port.placed == []  # vetoed before send (risk reasons are logged)


@pytest.mark.asyncio
async def test_halt_state_blocks_entries():
    r = CreditSpreadRunner(SpreadCfg())
    port = StubPort()
    r.port = port
    acct = await port.account()
    r.risk.check_pnl_limits("t", Decimal("-60"), Decimal("0"))  # halt!
    intent = OrderIntent(
        algo="credit_spread",
        product_symbol="C-BTC-79600-251026",
        side=Side.SELL,
        size=5,
        purpose="spread_short",
    )
    r.quotes = {intent.product_symbol: _quote()}
    ok = await r._send(intent, acct, veto=True)  # noqa: SLF001
    assert not ok and port.placed == []  # halted_by_circuit_breaker
