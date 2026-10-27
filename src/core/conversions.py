"""Unit conversions and sanity guards.

Premiums on Delta are quoted **per coin**; contract_value is in coins per
contract (e.g. 0.001 for BTC options). Earlier bug class: treating a
per-coin premium as a per-contract USD figure produced 1000x errors.
Every conversion is centralised here; no other module multiplies raw numbers.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

ZERO = Decimal("0")


def premium_usd(
    premium_per_coin: Decimal, contract_value: Decimal, size: int
) -> Decimal:
    """USD value of `size` contracts quoted at `premium_per_coin`.

    raises ValueError if the inputs are negative or if size is not a
    positive integer — catches the class of 1000x / negative-size bugs.
    """
    if size != int(size) or size <= 0:
        raise ValueError(f"size must be a positive integer, got {size!r}")
    if premium_per_coin < ZERO or contract_value <= ZERO:
        raise ValueError(
            "premium and contract_value must be 0<=p, cv>0: "
            f"{premium_per_coin}, {contract_value}"
        )
    return premium_per_coin * contract_value * size


def notional_usd(
    underlying_price: Decimal, contract_value: Decimal, size: int
) -> Decimal:
    """USD notional of `size` contracts at `underlying_price`."""
    if size <= 0:
        raise ValueError(f"size must be > 0, got {size!r}")
    if underlying_price <= ZERO or contract_value <= ZERO:
        raise ValueError("price/contract_value must be positive")
    return underlying_price * contract_value * size


def contracts_for_budget(
    budget_usd: Decimal, premium_per_coin: Decimal, contract_value: Decimal
) -> int:
    """Largest contract count whose premium cost keeps within budget_usd.

    Never returns a negative number; returns 0 when not even one contract fits.
    """
    if budget_usd <= ZERO or premium_per_coin <= ZERO or contract_value <= ZERO:
        return 0
    cost = premium_per_coin * contract_value
    return max(0, int((budget_usd / cost).to_integral_value(ROUND_FLOOR)))


def rollover_factor(premium_a: Decimal, premium_b: Decimal) -> Decimal:
    """Credit factor when rolling from premium_a (old short) to premium_b."""
    if premium_a <= ZERO:
        raise ValueError("old premium must be > 0")
    return premium_b / premium_a
