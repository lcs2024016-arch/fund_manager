"""Option payoff, settlement and defined-risk spread math.

Everything is pure and deterministic. Settlement uses the underlying index
price at expiry (Delta has no settle-price endpoint — see API_NOTES.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

ZERO = Decimal("0")


def payoff(kind: str, strike: Decimal, settle: Decimal) -> Decimal:
    """Intrinsic payoff per coin held (0 when worthless)."""
    if settle < ZERO:
        raise ValueError("settle < 0")
    if kind == "call":
        return max(settle - strike, ZERO)
    if kind == "put":
        return max(strike - settle, ZERO)
    raise ValueError(f"kind must be call|put, got {kind!r}")


def settle_pnl_short(
    kind: str, strike: Decimal, settle: Decimal, credit_per_coin: Decimal
) -> Decimal:
    """Realised per-coin P&L after settlement for a short leg."""
    return credit_per_coin - payoff(kind, strike, settle)


def settle_pnl_long(
    kind: str, strike: Decimal, settle: Decimal, debit_per_coin: Decimal
) -> Decimal:
    return payoff(kind, strike, settle) - debit_per_coin


@dataclass(frozen=True)
class SpreadLeg:
    kind: str  # "call" | "put"
    side: str  # "sell" (short) | "buy" (long)
    strike: Decimal
    premium_per_coin: Decimal
    size: int = 1

    def is_short(self) -> bool:
        return self.side == "sell"


@dataclass(frozen=True)
class CreditSpread:
    """Short vertical spread: short + long SAME kind, long further out.

    Defined risk: max loss per coin = width - net credit. This structure
    replaces the leader's naked option shorts (REPORT.md §2/§5 finding:
    uncapped short-vol carries a >230x-daily-premium tail).
    """

    kind: str  # "call" (short upside) | "put" (short downside)
    short: SpreadLeg
    long: SpreadLeg
    expiry: str  # e.g. "2026-10-24" — same on both legs

    def validate(self) -> None:
        if self.short.kind != self.long.kind or self.kind != self.short.kind:
            raise ValueError("legs must be same kind as spread")
        if not self.short.is_short():
            raise ValueError("short leg must be the short side")
        if self.long.is_short():
            raise ValueError("long leg must be the long side")
        if self.short.size != self.long.size or self.short.size <= 0:
            raise ValueError("leg sizes must match and be > 0")
        if (self.kind == "call") != (self.long.strike > self.short.strike):
            raise ValueError(
                "long strike beyond short strike (call: long above, put: long below)"
            )

    def size(self) -> int:
        return self.short.size

    def width_per_coin(self) -> Decimal:
        return abs(self.long.strike - self.short.strike)

    def net_credit_per_coin(self) -> Decimal:
        return self.short.premium_per_coin - self.long.premium_per_coin

    def net_credit_usd(self, contract_value: Decimal) -> Decimal:
        return self.net_credit_per_coin() * contract_value * self.size()

    def max_loss_per_coin(self) -> Decimal:
        return max(self.width_per_coin() - self.net_credit_per_coin(), ZERO)

    def max_loss_usd(self, contract_value: Decimal) -> Decimal:
        return self.max_loss_per_coin() * contract_value * self.size()

    def max_profit_usd(self, contract_value: Decimal) -> Decimal:
        return self.net_credit_usd(contract_value)

    def settle_pnl_usd(self, settle: Decimal, contract_value: Decimal) -> Decimal:
        """Realised P&L of both legs settled at `settle` (credit + legs)."""
        size = self.size()
        short_v = payoff(self.kind, self.short.strike, settle) * size
        long_v = payoff(self.kind, self.long.strike, settle) * size
        # premium cash: +credit when sold; long premium already netted inside credit
        return (self.net_credit_per_coin() * size - short_v + long_v) * contract_value

    def open_loss_usd(
        self,
        spot: Decimal,
        short_mark: Decimal,
        long_mark: Decimal,
        contract_value: Decimal,
    ) -> Decimal:
        """Mark-to-market loss if bought back now (<0 loss, >0 profit)."""
        size = self.size()
        value_now = (short_mark - long_mark) * size * contract_value  # buyback cost
        return self.net_credit_usd(contract_value) - value_now

    def strikes_below(self, spot: Decimal, step: Decimal, n: int) -> Decimal:
        """n-th strike below spot on a `step` grid (never below 0)."""
        if step <= ZERO or n < 0 or spot <= ZERO:
            raise ValueError("bad grid args")
        k = ((spot / step).to_integral_value(rounding=ROUND_FLOOR) - n) * step
        return max(k, ZERO)
