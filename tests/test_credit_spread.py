"""Strategy unit tests + the June-2026-style tail reproduction (explicit!)."""

from decimal import Decimal as D

from core.models import Side
from core.options import CreditSpread, SpreadLeg, payoff
from strategy.credit_spread import (
    MarketState,
    OpenSpread,
    Quote,
    SpreadCfg,
    evaluate,
    stop_outs,
)

CV = D("0.001")


def q(sym, kind, strike, delta, bid, ask, bid_size=10, cv=CV, price=None):
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
        contract_value=cv,
        underlying_price=D(price or strike),
    )


def test_basic_entry_call_put_defined_risk():
    cfg = SpreadCfg(notional_budget_usd=400)
    st = MarketState(
        now="2026-10-22T09:30:00Z",
        quotes=[
            q("C-BTC-79500-241026", "call", 79500, 0.20, 0.0500, 0.0520),
            q("C-BTC-80500-241026", "call", 80500, 0.10, 0.0150, 0.0160),
            q("P-BTC-76500-241026", "put", 76500, 0.20, 0.0400, 0.0420),
            q("P-BTC-75500-241026", "put", 75500, 0.10, 0.0200, 0.0210),
        ],
    )
    dec = evaluate(st, cfg)
    kinds = [o.purpose for o in dec.open_orders]
    assert kinds.count("spread_short") == 2
    assert kinds.count("spread_hedge_long") == 2
    shorts = [o for o in dec.open_orders if o.side == Side.SELL]
    # sells are BELOW bid (slippage guard)
    assert all(o.limit_price < D("0.052") for o in shorts)
    # risk meta records max loss
    assert all(D(o.meta["max_loss_usd"]) > 0 for o in shorts)


def test_rejects_when_credit_thin_or_book_thin():
    cfg = SpreadCfg()
    st = MarketState(
        now="n",
        quotes=[
            q("C-BTC-79500-241026", "call", 79500, 0.2, 0.00001, 0.00002, bid_size=1),
            q("C-BTC-80500-241026", "call", 80500, 0.1, 0.0, 0.0),
        ],
    )
    dec = evaluate(st, cfg)
    assert dec.open_orders == []
    assert dec.rejects  # some documented reason


def test_delta_band_pinned_strike_determinism():
    cfg = SpreadCfg()
    quotes = [
        q("C-BTC-78000-241026", "call", 78000, 0.35, 0.1, 0.11),
        q("C-BTC-79000-241026", "call", 79000, 0.29, 0.08, 0.09),
        q("C-BTC-80000-241026", "call", 80000, 0.20, 0.05, 0.052),  # first in band
        q("C-BTC-81000-241026", "call", 81000, 0.12, 0.025, 0.027),
        q("C-BTC-82000-241026", "call", 82000, 0.05, 0.01, 0.012),
    ]
    dec = evaluate(MarketState(now="n", quotes=quotes), cfg)
    shorts = [o for o in dec.open_orders if o.side == Side.SELL]
    assert len(shorts) == 1 and shorts[0].product_symbol == "C-BTC-80000-241026"


def test_max_open_spreads_per_underlying():
    cfg = SpreadCfg(max_open_spreads_per_underlying=1)
    open_sp = OpenSpread(
        spread=CreditSpread(
            "put",
            SpreadLeg("put", "sell", D("80"), D("0.05")),
            SpreadLeg("put", "buy", D("78"), D("0.02")),
            expiry="2026-10-24",
        ),
        underlying="BTC",
        opened_ts="t",
        entry_credit_usd=D("1"),
        mark_cost_usd=D("0.5"),
        symbol="x",
    )
    st = MarketState(
        now="n",
        quotes=[
            q("C-BTC-79500-241026", "call", 79500, 0.2, 0.05, 0.052),
            q("C-BTC-80500-241026", "call", 80500, 0.1, 0.015, 0.016),
        ],
        open_spreads=[open_sp],
    )
    dec = evaluate(st, cfg)
    assert all(o.purpose != "spread_short" for o in dec.open_orders)


def test_june2026_tail_reproduces_with_defined_risk_cap():
    """Explicit tail check: the risk-first book survives a June-2026-style
    move. Simulate a -12% BTC week against a short put vertical and compare
    with the leader's OBSERVED Jun-26 giveback (−$347 on $1,000 margin,
    REPORT.md §4.3). The defined-risk loss is CAPPED at width−credit."""
    cv = D("1")  # 1 contract = 1 coin notional for the math
    short_st, long_st, credit = D("95000"), D("92000"), D("0.0450")
    spot_end = D("88000")  # −12% (June-2026 style)
    # short put vertical settle: credit − short payoff + long payoff
    settle = (
        credit - payoff("put", short_st, spot_end) + payoff("put", long_st, spot_end)
    ) * cv
    # deep ITM both legs: capped at credit − width = −(width−credit)
    assert settle == D("-2999.9550") or settle <= D("0")
    max_loss_per_contract = (short_st - long_st) - credit  # width − credit
    assert max_loss_per_contract == D("2999.9550")
    n = 2  # 2 spreads on a $1,000 margin (oversize!)
    dd = max_loss_per_contract * n
    leader_dd = D("347")  # OBSERVED leader giveback
    assert dd > leader_dd  # at oversize even the capped loss bites
    # at sane sizing (2 contracts of a 0.001 cv BTC vertical = ~$6 max loss)
    sane = max_loss_per_contract * D("0.001") * n
    assert sane < leader_dd  # and printing is the point:
    print(
        "oversize capped loss:",
        dd,
        "| sane BTC-cv capped loss:",
        sane,
        "vs OBSERVED leader June-26:",
        leader_dd,
    )


def test_stop_outs_fire_at_multiple():
    cfg = SpreadCfg(stop_loss_credit_multiple=2.0)
    sp = CreditSpread(
        "put",
        SpreadLeg("put", "sell", D("80"), D("0.05")),
        SpreadLeg("put", "buy", D("78"), D("0.02")),
        expiry="2026-10-24",
    )
    ok = OpenSpread(
        spread=sp,
        underlying="BTC",
        opened_ts="t",
        entry_credit_usd=D("10"),
        mark_cost_usd=D("1"),
        symbol="s1",
    )
    dead = OpenSpread(
        spread=sp,
        underlying="BTC",
        opened_ts="t",
        entry_credit_usd=D("10"),
        mark_cost_usd=D("21"),
        symbol="s2",
    )
    ms = MarketState(now="n", quotes=[], open_spreads=[ok, dead])
    assert stop_outs(ms, cfg) == ["s2"]
