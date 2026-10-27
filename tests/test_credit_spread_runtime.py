"""Runtime-adapter coverage for the credit-spread runner: bootstrap,
quote collection, open-spread sync, stop-outs, entries, fills, reconcile
halt reporting and silent-risk paths. Complements test_credit_spread.py
(pure core) and test_credit_spread_runner.py (veto path)."""

from __future__ import annotations

import asyncio
from decimal import Decimal as D

import pytest

from core.models import (
    AccountSnapshot,
    Candle,
    FillEvent,
    OrderIntent,
    OrderRecord,
    OrderResult,
    OrderStatus,
    PositionSnapshot,
    ProductInfo,
    Side,
    Ticker,
)
from strategy.credit_spread import (
    CreditSpreadRunner,
    CreditSpreadStrategy,
    Decision,
    MarketState,
    Quote,
    SpreadCfg,
    _parse_option_symbol,
    evaluate,
    json_dump,
)


def _products() -> list[ProductInfo]:
    out = []
    for sym, kind in (
        ("C-BTC-79500-241026", "call_options"),
        ("C-BTC-80500-241026", "call_options"),
        ("C-BTC-80000-241026", "call_options"),
        ("P-BTC-76500-241026", "put_options"),
        ("P-BTC-75500-241026", "put_options"),
    ):
        out.append(
            ProductInfo(
                symbol=sym,
                product_id=abs(hash(sym)) % 100000,
                contract_type=kind,
                tick_size=D("0.0001"),
                contract_value=D("0.001"),
                strike_price=D(sym.split("-")[2]),
                underlying_asset_symbol="BTC",
            )
        )
    return out


class RuntimeStubPort:
    """Paper-port-shaped stub exercising the full runner loop."""

    def __init__(self, accept: bool = True, fail_place: bool = False) -> None:
        self.accepted = accept
        self.fail_place = fail_place
        self.placed: list[OrderIntent] = []

    async def product(self, symbol: str):
        for p in _products():
            if p.symbol == symbol:
                return p
        return None

    async def products(self):
        return _products()

    async def place(self, intent: OrderIntent):
        if self.fail_place:
            raise RuntimeError("book closed")
        if not self.accepted:
            return OrderResult(client_order_id=intent.client_order_id, accepted=False)
        self.placed.append(intent)
        return OrderResult(client_order_id=intent.client_order_id, accepted=True)

    async def account(self):
        return AccountSnapshot(
            equity=D("100000"),
            wallet_balance=D("100000"),
            available_balance=D("99000"),
            used_margin=D("1000"),
        )

    async def position(self, symbol: str):
        return PositionSnapshot(
            symbol=symbol, product_id=1, net_size=-1, mark_price=D("0.05")
        )

    async def open_orders(self):
        return []

    async def ticker(self, symbol: str):
        return Ticker(
            symbol=symbol,
            best_bid=D("0.0500"),
            best_ask=D("0.0502"),
            spot_price=D("104000"),
            delta=D("-0.2"),
        )


QUOTE = Quote(
    symbol="/",
    underlying="BTC",
    kind="call",
    strike=D(0),
    expiry="2026-10-24",
    delta=0.2,
    best_bid=D(0),
    best_ask=D(0),
    bid_size=10,
    contract_value=D("0.001"),
    underlying_price=D("104000"),
)


def _opt(
    sym: str,
    kind: str,
    strike: str,
    delta: float,
    bid: str,
    ask: str,
    bid_size: int = 10,
) -> Quote:
    return Quote(
        symbol=sym,
        underlying="BTC",
        kind=kind,
        strike=D(strike),
        expiry="2026-10-24",
        delta=delta,
        best_bid=D(bid),
        best_ask=D(ask),
        bid_size=bid_size,
        contract_value=D("0.001"),
        underlying_price=D("104000"),
    )


# ---------------------------------------------------------------------------
# config validation (lines 52/54/59)
# ---------------------------------------------------------------------------
def test_cfg_validate_rejects_bad_bands():
    with pytest.raises(ValueError, match="delta band empty"):
        cfg = SpreadCfg(short_delta_low=0.3, short_delta_high=0.3)
        cfg._validate()
    with pytest.raises(ValueError, match="XAUT"):
        cfg = SpreadCfg(underlyings=["XAUTUSD"], max_spread_pct=0.05)
        cfg._validate()
    with pytest.raises(ValueError, match="stop"):
        cfg = SpreadCfg(stop_loss_credit_multiple=0)
        cfg._validate()


# ---------------------------------------------------------------------------
# every pure-core reject branch (lines 167-206)
# ---------------------------------------------------------------------------
def test_evaluate_reject_branches_are_labelled():
    cfg = SpreadCfg()

    def run(quotes: list[Quote]) -> list[str]:
        dec = evaluate(MarketState(now="n", quotes=quotes), cfg)
        assert dec.open_orders == []
        return dec.rejects

    # thin short book (bid_size < 1)
    assert any(
        "thin_book" in r
        for r in run(
            [
                _opt(
                    "C-BTC-79500-241026",
                    "call",
                    "79500",
                    0.2,
                    "0.05",
                    "0.052",
                    bid_size=0,
                ),
                _opt("C-BTC-80500-241026", "call", "80500", 0.1, "0.015", "0.016"),
            ]
        )
    )
    # dead long quote (ask 0)
    assert any(
        "dead_quote" in r
        for r in run(
            [
                _opt("C-BTC-79500-241026", "call", "79500", 0.2, "0.05", "0.052"),
                _opt("C-BTC-80500-241026", "call", "80500", 0.1, "0.015", "0"),
            ]
        )
    )
    # wide short spread (> max_spread_pct 5%)
    assert any(
        "spread_wide" in r
        for r in run(
            [
                _opt("C-BTC-79500-241026", "call", "79500", 0.2, "0.04", "0.06"),
                _opt("C-BTC-80500-241026", "call", "80500", 0.1, "0.015", "0.016"),
            ]
        )
    )
    # net debit instead of credit (narrow short book, expensive long ask)
    assert any(
        "no_net_credit" in r
        for r in run(
            [
                _opt("C-BTC-79500-241026", "call", "79500", 0.2, "0.0100", "0.0102"),
                _opt("C-BTC-80500-241026", "call", "80500", 0.1, "0.0001", "0.0160"),
            ]
        )
    )
    # credit positive but below 2x the (min-capped) fee estimate
    assert any(
        "credit_too_thin" in r
        for r in run(
            [
                _opt("C-BTC-79500-241026", "call", "79500", 0.2, "0.0100", "0.0102"),
                _opt("C-BTC-80500-241026", "call", "80500", 0.1, "0.0001", "0.0096"),
            ]
        )
    )


def test_strategy_plugin_frames_evaluate():
    st = CreditSpreadStrategy(SpreadCfg())
    quotes = [
        _opt("C-BTC-79500-241026", "call", "79500", 0.2, "0.05", "0.052"),
        _opt("C-BTC-80500-241026", "call", "80500", 0.1, "0.015", "0.016"),
    ]
    dec: Decision = st.on_market_state(MarketState(now="n", quotes=quotes))
    assert dec.open_orders and "credit_spread" in dec.open_orders[0].algo


# ---------------------------------------------------------------------------
# runner runtime loop
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bootstrap_builds_option_universe():
    r = CreditSpreadRunner(SpreadCfg())
    await r.bootstrap(RuntimeStubPort())
    assert r._option_syms["BTCUSD"], r._option_syms  # noqa: SLF001
    assert not any(s in r._option_syms["BTCUSD"] for s in ("C-BTC-X", "BTCUSD"))


def test_spots_update_from_data_events():
    r = CreditSpreadRunner(SpreadCfg())
    asyncio.run(
        r.on_candle_close(
            Candle(
                symbol="BTCUSD",
                resolution="1m",
                ts=0,
                open=0,
                high=0,
                low=0,
                close=104000,
            )
        )
    )
    assert r.spots["BTCUSD"] == D(104000)
    asyncio.run(r.on_ticker("ETHUSD", Ticker(symbol="ETHUSD", spot_price=D("3100"))))
    assert r.spots["ETHUSD"] == D(3100)
    asyncio.run(r.on_ticker("C-BTC-79500-241026", Ticker(symbol="x")))
    assert "C-BTC-79500-241026" not in r.spots  # option quotes never set spot


def test_on_fill_unknown_is_noop_and_mirror_fill_applies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # _report_halt writes ./state/soak_mismatches.json
    r = CreditSpreadRunner(SpreadCfg())
    intent = OrderIntent(
        algo="credit_spread",
        product_symbol="C-BTC-79500-241026",
        side=Side.SELL,
        size=1,
        purpose="spread_short",
    )
    fill = FillEvent(
        client_order_id=intent.client_order_id,
        symbol=intent.product_symbol,
        side=Side.SELL,
        size=1,
        price=D("0.05"),
    )
    # unknown fill -> no local record -> ignored, no halt
    asyncio.run(r.on_fill(fill))
    assert r._halts_reported == 0
    # known order -> mirror applies the fill without a halt
    r.book.submit(
        OrderRecord(
            client_order_id=intent.client_order_id,
            symbol=intent.product_symbol,
            side=Side.SELL,
            size=1,
            status=OrderStatus.NEW,
        )
    )
    asyncio.run(r.on_fill(fill))
    assert r._halts_reported == 0
    assert r.book.orders[intent.client_order_id].filled_size == 1
    # direct halt report persists to the gate journal
    r._report_halt("apply_fill", ["boom"])
    assert r._halts_reported == 1 and (tmp_path / "state/soak_mismatches.json").exists()
    assert r.status()["unresolved"] == 1


@pytest.mark.asyncio
async def test_on_timer_places_entry_pair_and_records_it():
    r = CreditSpreadRunner(SpreadCfg(notional_budget_usd=10))
    port = RuntimeStubPort()
    await r.bootstrap(port)
    r.spots["BTCUSD"] = D("104000")
    shorts, longs = (
        _opt("C-BTC-79500-241026", "call", "79500", 0.2, "0.0500", "0.0520"),
        _opt("C-BTC-80500-241026", "call", "80500", 0.1, "0.0150", "0.0155"),
    )
    r.quotes = {shorts.symbol: shorts, longs.symbol: longs}
    await r.on_timer()
    placed = {o.product_symbol: o for o in port.placed}
    assert "C-BTC-79500-241026" in placed and "C-BTC-80500-241026" in placed
    short_sent = placed["C-BTC-79500-241026"]
    assert short_sent.side is Side.SELL and short_sent.reduce_only is False
    assert short_sent.limit_price <= D("0.052")  # sold below book (slip guard)
    pair = r._pairs["C-BTC-79500-241026"]  # noqa: SLF001
    assert pair["paired_long_leg"] == "C-BTC-80500-241026"
    assert pair["entry_credit_usd"] > 0
    assert r.status()["pairs"] == ["C-BTC-79500-241026"]  # synced next tick


@pytest.mark.asyncio
async def test_on_timer_fire_stop_outs_reduce_only():
    cfg = SpreadCfg(stop_loss_credit_multiple=2.0, notional_budget_usd=50)
    r = CreditSpreadRunner(cfg)
    port = RuntimeStubPort()
    await r.bootstrap(port)
    # open pair whose MTM cost explodes above 2x entry credit
    shorts, longs = (
        _opt("C-BTC-79500-241026", "call", "79500", 0.2, "0.0500", "0.1800"),
        _opt("C-BTC-80500-241026", "call", "80500", 0.1, "0.0150", "0.0200"),
    )
    r.quotes = {shorts.symbol: shorts, longs.symbol: longs}
    r._pairs["C-BTC-79500-241026"] = {  # noqa: SLF001
        "spread": None,
        "entry_credit_usd": D("0.00001"),
        "size": 1,
        "paired_long_leg": "C-BTC-80500-241026",
        "underlying": "BTC",
        "opened_ts": "2026-10-24",
    }
    await r.on_timer()
    stops = [o for o in port.placed if o.purpose == "spread_stop"]
    assert {o.product_symbol for o in stops} == {
        "C-BTC-79500-241026",
        "C-BTC-80500-241026",
    }
    assert all(o.reduce_only for o in stops)
    assert not r._pairs  # pair bookkeeping cleared after stop-out  # noqa: SLF001


@pytest.mark.asyncio
async def test_collect_quotes_filters_out_of_band_and_adds_fresh_books():
    import datetime

    today = datetime.datetime.now(datetime.UTC)
    dmy = today.strftime("%d%m%y")
    r = CreditSpreadRunner(SpreadCfg())
    port = RuntimeStubPort()
    await r.bootstrap(port)
    # expired daily, live near-spot, far-out strike, far-away expiry
    syms = ["C-BTC-79500-241026", f"C-BTC-103000-{dmy}", "C-BTC-200000-301231"]
    r._option_syms = {"BTCUSD": syms}  # noqa: SLF001
    r.spots["BTCUSD"] = D("104000")
    stale = Quote(
        symbol="stale",
        underlying="BTC",
        kind="call",
        strike=D(1),
        expiry="2026-10-24",
        delta=0,
        best_bid=D(1),
        best_ask=D(1),
        bid_size=1,
        contract_value=D("0.001"),
        underlying_price=D("0"),
    )
    r.quotes = {stale.symbol: stale}
    await r._collect_quotes()
    fresh = set(r.quotes)  # noqa: SLF001
    # live near-spot + same-day daily collected; expired / far strike / far
    # expiry dropped; alive underlying (MTM) quote kept
    assert f"C-BTC-103000-{dmy}" in fresh
    for gone in ("C-BTC-79500-241026", "C-BTC-200000-301231"):
        assert gone not in fresh
    assert "stale" in fresh  # underlying book is preserved
    q = r.quotes[f"C-BTC-103000-{dmy}"]  # noqa: SLF001
    assert q.best_bid == D("0.0500") and q.contract_value == D("0.001")
    assert _parse_option_symbol(f"C-BTC-103000-{dmy}") is not None


@pytest.mark.asyncio
async def test_sync_open_spreads_prunes_flat_and_marks_alive():
    r = CreditSpreadRunner(SpreadCfg())
    port = RuntimeStubPort()
    await r.bootstrap(port)
    r._pairs = {  # one flat (port returns net 0 -> prune happens via position None)
        "C-BTC-79500-241026": {
            "spread": None,
            "entry_credit_usd": D("0.05"),
            "size": 1,
            "paired_long_leg": "C-BTC-80500-241026",
            "underlying": "BTC",
            "opened_ts": "2026-10-24",
        },
    }
    shorts, longs = (
        _opt("C-BTC-79500-241026", "call", "79500", 0.2, "0.0500", "0.0520"),
        _opt("C-BTC-80500-241026", "call", "80500", 0.1, "0.0060", "0.0160"),
    )
    r.quotes = {shorts.symbol: shorts, longs.symbol: longs}
    await r._sync_open_spreads()
    assert len(r.open_spreads) == 1  # position() returns -1, still alive
    sp = r.open_spreads[0]
    assert sp.entry_credit_usd == D("0.05")
    assert sp.mark_cost_usd == ((D("0.0520") - D("0.0060")) * D("0.001"))

    class FlatPort(RuntimeStubPort):
        async def position(self, symbol):
            return None  # short leg flat -> pair dead

    r2 = CreditSpreadRunner(SpreadCfg())
    r2.port = FlatPort()
    r2._pairs = dict(r._pairs)  # noqa: SLF001
    r2.quotes = r.quotes
    await r2._sync_open_spreads()
    assert r2.open_spreads == [] and not r2._pairs  # noqa: SLF001


@pytest.mark.asyncio
async def test_send_missing_context_and_place_failures():
    r = CreditSpreadRunner(SpreadCfg())
    port = RuntimeStubPort()
    r.port = port
    acct = await port.account()
    intent = OrderIntent(
        algo="credit_spread",
        product_symbol="C-BTC-79500-241026",
        side=Side.SELL,
        size=1,
        purpose="spread_short",
    )
    assert not await r._send(intent, acct, veto=True)  # no quote -> False
    r.quotes = {
        "C-BTC-79500-241026": _opt(
            "C-BTC-79500-241026", "call", "79500", 0.2, "0.05", "0.052"
        )
    }
    port_missing = RuntimeStubPort()
    # product lookup fails during veto
    port_missing.product = lambda s: asyncio.sleep(0, result=None)  # type: ignore[assignment]
    r.port = port_missing
    assert not await r._send(intent, acct, veto=True)
    r.port = RuntimeStubPort(fail_place=True)
    assert not await r._send(intent, acct, veto=False)  # place raises -> False


@pytest.mark.asyncio
async def test_reconcile_reports_halt_for_local_only_open_orders():
    monkey_freezer = None  # noqa: F841
    r = CreditSpreadRunner(SpreadCfg())
    port = RuntimeStubPort()
    r.port = port
    r.book.submit(
        OrderRecord(  # locally open order, exchange lost it
            client_order_id="cs-btc-lost",
            symbol="C-BTC-79500-241026",
            side=Side.SELL,
            size=1,
            status=OrderStatus.NEW,
        )
    )
    await r._reconcile_book()
    assert r._halts_reported == 1
    assert r.status()["open_spreads"] == 0


def test_json_dump_helper(tmp_path):
    out = json_dump({"a": [1, 2]})
    assert '"a"' in out and "[\n" in out
    assert _parse_option_symbol("C-BTC-79500-2410262") is None
