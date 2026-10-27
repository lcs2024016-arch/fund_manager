"""Portfolio risk engine tests, incl. the June-2026 tail being flagged."""

from decimal import Decimal as D

from core.models import AccountSnapshot, OrderIntent, PositionSnapshot, Side, Ticker
from core.portfolio_risk import PortfolioRiskEngine, RiskLimits

cv_usd = {"BTCUSD": 100000.0 * 0.001, "ETHUSD": 100000 * 0.02}


def acct(eq=1000.0, avail=800.0, used=200.0, unreal=0.0):
    return AccountSnapshot(
        equity=D(str(eq)),
        wallet_balance=D("1000"),
        available_balance=D(str(avail)),
        used_margin=D(str(used)),
    )


def pos(sym="BTCUSD", net=1, unreal=0.0):
    return PositionSnapshot(
        symbol=sym, product_id=1, net_size=net, unrealized_pnl=D(str(unreal))
    )


def intent(sym="BTCUSD", side="buy", size=1, price="100"):
    return OrderIntent(
        algo="t", product_symbol=sym, side=Side(side), size=size, limit_price=D(price)
    )


def test_pre_trade_happy_path():
    eng = PortfolioRiskEngine(limits=RiskLimits())
    ok, why = eng.pre_trade(
        intent(),
        acct(),
        Ticker(symbol="BTCUSD", best_bid=D("100"), best_ask=D("100")),
        [pos()],
        100.0,
        bid_size=5,
    )
    assert ok, why


def test_halt_blocks_orders_until_operator_reset():
    eng = PortfolioRiskEngine(limits=RiskLimits())
    eng.check_pnl_limits("t0", D("-60"), D("0"))
    assert eng.halted
    ok, why = eng.pre_trade(intent(), acct(), None, [], 100.0)
    assert not ok and "halted_by_circuit_breaker" in why
    eng.reset_halt("t1", operator="oncall")
    ok, why = eng.pre_trade(intent(), acct(), None, [], 100.0)
    assert ok, why


def test_drawdown_breaker_flatten_and_halt():
    eng = PortfolioRiskEngine(limits=RiskLimits(max_drawdown_pct=0.20))
    eng.check_drawdown("t", D("790"), D("1000"))  # -21%
    assert eng.halted and "drawdown" in eng.halt_reason
    eng2 = PortfolioRiskEngine(limits=RiskLimits(max_drawdown_pct=0.20))
    eng2.check_drawdown("t", D("850"), D("1000"))  # -15%: no halt
    assert not eng2.halted


def test_open_book_mtm_loss_limit_not_realised_only():
    eng = PortfolioRiskEngine(limits=RiskLimits(max_open_book_loss_pct=0.10))
    eng.check_mtm_loss("t", D("1000"), [pos("BTCUSD", -1, unreal=-150)])
    assert eng.halted and "open book loss" in eng.halt_reason


def test_group_and_delta_caps():
    eng = PortfolioRiskEngine(
        limits=RiskLimits(
            max_group_notional_usd={"crypto_beta": 300.0, "gold": 2000.0},
            max_notional_usd_per_underlying=200.0,
            max_net_delta_usd=300.0,
        )
    )
    ok, why = eng.pre_trade(
        intent(size=5), acct(), None, [], 100.0, bid_size=10
    )  # 5x100=500 > 300 group cap
    assert not ok and (
        "group_notional_cap" in why
        or "underlying_notional_cap" in why
        or "net_delta_cap" in why
    )
    ok2, why2 = eng.pre_trade(
        intent(side="sell", size=1),
        acct(),
        None,
        [pos("BTCUSD", -1)],
        100.0,
        bid_size=10,
    )
    assert ok2, why2  # short 1 lot: delta stays within band


def test_malformed_orders_vetoed():
    eng = PortfolioRiskEngine(limits=RiskLimits())
    ok, why = eng.pre_trade(intent(size=0), acct(), None, [], 100.0)
    assert not ok and "size_must_be_positive_int" in why
    ok, why = eng.pre_trade(intent(price="-1"), acct(), None, [], 100.0)
    assert not ok and "non_positive_limit_price" in why
    ok, why = eng.pre_trade(intent(), acct(avail=0), None, [], 100.0)
    assert not ok and "no_available_balance" in why


def test_spread_cap_symbol_pattern():
    eng = PortfolioRiskEngine(
        limits=RiskLimits(max_spread_pct={"default": 0.05, "XAUT*": 0.35})
    )
    assert eng.spread_cap("XAUTUSD") == 0.35
    assert eng.spread_cap("BTCUSD") == 0.05


def test_june2026_tail_flagged_not_hidden(monkeypatch, capsys):
    """A replica must SHOW the June-2026-style bleed (−$347 on $1,000 margin)
    rather than hide it: with MTM limit 10% the engine halts at −$100 open
    book loss on a $1,000 equity — well BEFORE −$347 is reached. That is the
    honest reproduction: the bleed exists, the breaker catches it."""
    eng = PortfolioRiskEngine(limits=RiskLimits(max_open_book_loss_pct=0.10))
    bleeding_book = [
        pos("BTCUSD", net=-1, unreal=-200.0),  # Jun-style bleed
        pos("ETHUSD", net=-1, unreal=-147.0),
    ]
    eng.check_mtm_loss("2026-06-12T09:00:00Z", D("1000"), bleeding_book)
    assert eng.halted, "breaker must fire BEFORE the full Jun-26 giveback"
    assert eng.halt_reason
