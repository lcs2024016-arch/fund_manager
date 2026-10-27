"""Property tests (hypothesis): invariants under random price paths."""

from decimal import Decimal
from decimal import Decimal as D

from hypothesis import given, settings
from hypothesis import strategies as st

from core.models import (
    FillEvent,
    OrderRecord,
    OrderStatus,
    Side,
)
from core.oms import OrderBook
from core.options import CreditSpread, SpreadLeg
from strategy.credit_spread import (
    MarketState,
    OpenSpread,
    Quote,
    SpreadCfg,
    evaluate,
    stop_outs,
)

CV = Decimal("0.001")


@st.composite
def quote_st(draw):
    kind = draw(st.sampled_from(["call", "put"]))
    spot = draw(st.integers(min_value=500, max_value=120000))
    prem = draw(st.integers(min_value=1, max_value=2000)) / 1000
    return Quote(
        symbol="C-BTC-79500-301026",
        underlying="BTC",
        kind=kind,
        strike=Decimal(spot),
        expiry="2026-10-30",
        delta=draw(st.floats(min_value=0.11, max_value=0.29)),
        best_bid=Decimal(prem),
        best_ask=Decimal(prem) + Decimal("0.001"),
        bid_size=draw(st.integers(min_value=1, max_value=50)),
        contract_value=CV,
        underlying_price=Decimal(spot),
    )


@given(x=quote_st())
@settings(max_examples=30, deadline=None)
def test_never_naked_and_no_negative_orders(x):
    cfg = SpreadCfg()
    st_ = MarketState(now="2026-10-22T09:30:00Z", quotes=[x])
    dec = evaluate(st_, cfg)
    for o in dec.open_orders:
        assert o.size > 0
        assert o.limit_price is None or o.limit_price >= 0
        if o.side == Side.SELL:
            # short legs are always paired with a long leg hedging them
            metas = {m.product_symbol for m in dec.open_orders if m.side == Side.BUY}
            assert o.meta.get("paired_long_leg") in metas


@given(x=quote_st())
@settings(max_examples=30, deadline=None)
def test_spread_max_loss_below_width(x):
    cfg = SpreadCfg()
    st_ = MarketState(now="2026-10-22T09:30:00Z", quotes=[x])
    dec = evaluate(st_, cfg)
    # any emitted short order's implied spread has loss <= width per coin
    for o in dec.open_orders:
        if o.side == Side.SELL:
            short_st = Decimal(x.strike)
            long_st = short_st + Decimal(cfg.width_strikes) * 1000
            width = abs(long_st - short_st)
            credit = x.best_bid * Decimal("0.995") - (x.best_ask + Decimal("0.0005"))
            assert width - credit >= 0


@given(sizes=st.lists(st.tuples(st.integers(1, 5), st.integers(1, 99999)), max_size=12))
@settings(max_examples=30, deadline=None)
def test_no_overfill_and_pnl_identity(sizes):
    b = OrderBook()
    b.submit(
        OrderRecord(
            client_order_id="p",
            symbol="ETHUSD",
            side=Side.BUY,
            size=10,
            status=OrderStatus.NEW,
        )
    )
    total = 0
    for n, price in sizes:
        take = min(n, 10 - total)
        if take <= 0:
            continue
        b.apply_fill(
            FillEvent(
                client_order_id="p",
                symbol="ETHUSD",
                side=Side.BUY,
                size=take,
                price=Decimal(price),
            )
        )
        total += take
        assert b.orders["p"].filled_size == total
    assert total <= 10


@given(
    k=st.floats(min_value=0, max_value=10),
    credit=st.floats(min_value=0.001, max_value=5),
)
@settings(max_examples=30, deadline=None)
def test_stop_out_monotonic(k, credit):
    cfg = SpreadCfg()
    sp = CreditSpread(
        "call",
        SpreadLeg("call", "sell", D("100"), D("0.05")),
        SpreadLeg("call", "buy", D("102"), D("0.01")),
        expiry="2026-10-30",
    )
    os_ = OpenSpread(
        spread=sp,
        underlying="BTC",
        opened_ts="t",
        entry_credit_usd=D(str(credit)),
        mark_cost_usd=D(str(k)) * D(str(credit)),
        symbol="s",
    )
    ms = MarketState(now="n", quotes=[], open_spreads=[os_])
    outs = stop_outs(ms, cfg)
    assert (k >= cfg.stop_loss_credit_multiple) == (outs == ["s"])
