"""Unit tests: conversions + fee math + option payoff/settlement/spreads."""

from decimal import Decimal as D

import pytest

from core.conversions import contracts_for_budget, notional_usd, premium_usd
from core.fees import linear_fee, option_fee
from core.options import CreditSpread, SpreadLeg, payoff, settle_pnl_short

CV = D("0.001")  # BTC option contract value


def test_premium_usd_matches_per_coin_semantics():
    # premium quoted in coins-not-USD: 0.1 BTC-prem x 0.001 cv x 2 = 0.0002
    assert premium_usd(D("0.1"), CV, 2) == D("0.0002")


def test_premium_usd_catches_1000x_bug_class():
    with pytest.raises(ValueError):
        premium_usd(D("-0.1"), CV, 2)  # negative premium
    with pytest.raises(ValueError):
        premium_usd(D("0.1"), D("-0.001"), 2)
    for bad in (0, -1, 2.5):
        with pytest.raises(ValueError):
            premium_usd(D("0.1"), CV, bad)  # non-positive/float size


def test_notional_and_budget():
    assert notional_usd(D("100000"), CV, 1) == D("100.000")
    # $10 budget, premium $0.05/coin * 0.001 cv = $0.00005/contract
    assert contracts_for_budget(D("10"), D("0.05"), CV) == 200000


def test_option_fee_schedule_verified_form():
    cv = D("1")  # per-contract premium 0.05, notional 100
    u, prem = D("100"), D("0.05")
    f1 = option_fee(u, prem, cv, 1)
    assert f1 == min(D("0.0001") * D("100"), D("0.035") * D("0.05")) * D("1.18")
    # cap binds for tiny premiums (cv 0.001: premium $0.00005, notional $100)
    f2 = option_fee(D("100000"), D("0.05"), CV, 1)
    assert f2 == min(D("0.01"), D("0.035") * D("0.00005")) * D("1.18")
    assert f2 == D("0.00000175") * D("1.18")


def test_linear_fee_unverified_defaults():
    x = linear_fee(D("1000"), side_taker=True)
    assert x == D("1000") * D("0.0020") * D("1.18")
    assert linear_fee(D("0"), True) == 0


def test_payoff_and_settlement():
    assert payoff("call", D("100"), D("110")) == D("10")
    assert payoff("put", D("100"), D("90")) == D("10")
    assert payoff("put", D("100"), D("120")) == 0
    with pytest.raises(ValueError):
        payoff("cassowary", D("1"), D("1"))
    with pytest.raises(ValueError):
        payoff("call", D("1"), D("-1"))
    # short call: credit 0.05, settle 104, strike 100 -> -3.95 per coin
    assert settle_pnl_short("call", D("100"), D("104"), D("0.05")) == D("-3.95")


def _spread(short_prem: str, long_prem: str = None):
    short = SpreadLeg("call", "sell", D("100"), D(short_prem))
    long = SpreadLeg("call", "buy", D("102"), D(long_prem or "0.01"))
    return CreditSpread("call", short, long, expiry="2026-10-24")


def test_credit_spread_defined_risk_validations():
    sp = _spread("0.05")
    sp.validate()
    assert sp.width_per_coin() == D("2")
    assert sp.net_credit_per_coin() == D("0.04")
    assert sp.max_loss_per_coin() == D("1.96")
    assert sp.net_credit_usd(CV) == D("0.00004")
    # bad shapes raise: a put vertical wrongly built with the long ABOVE
    with pytest.raises(ValueError):
        CreditSpread(
            "put",
            SpreadLeg("put", "sell", D("100"), D("0.05")),
            SpreadLeg("put", "buy", D("102"), D("0.01")),
            expiry="2026-10-24",
        ).validate()
    bad = CreditSpread(
        "call",
        SpreadLeg("call", "buy", D("100"), D("0.05")),
        SpreadLeg("call", "buy", D("102"), D("0.01")),
        expiry="2026-10-24",
    )
    with pytest.raises(ValueError):
        bad.validate()


def test_credit_spread_settle_pnl_every_region():
    sp = _spread("0.05")
    cv = D("1")
    # below both strikes: keep credit (0.04/coin)
    assert sp.settle_pnl_usd(D("90"), cv) == D("0.04")
    # between: credit - (settle-100) = 0.04 - 1 - 0 = -0.96
    assert sp.settle_pnl_usd(D("101"), cv) == D("-0.96")
    # above both: width-loss region = credit - 2 = -1.96 -> capped by long
    assert sp.settle_pnl_usd(D("105"), cv) == D("-1.96")


def test_open_loss_mtm_marking():
    sp = _spread("0.05")
    # buyback costs (short_mark - long_mark): 0.10/coin vs credit 0.04 -> -0.06
    assert sp.open_loss_usd(D("101"), D("0.10"), D("0"), D("1")) == D("-0.06")
