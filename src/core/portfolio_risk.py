"""Portfolio risk engine — limits, pre-trade checks, MTM loss limits and a
drawdown circuit breaker (flatten + halt).

RULES OF ENGAGEMENT (RISK.md §2):
- Every position must have a defined max loss; naked option shorts forbidden.
- Mark-to-market (the open book) is what is measured, not realised-only.
- Per-asset caps AND a correlated-group cap (BTC+ETH move together; XAUT+PAXG
  is the gold group).
- Breach => engine sets `halted=True`; the trading loop MUST flatten all
  positions (reduce-only) and stop. Only an operator can reset (kill-switch
  parity: `python -m tools.killswitch reset`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, Field

from core.models import AccountSnapshot, OrderIntent, PositionSnapshot, Ticker

ZERO = Decimal("0")

GROUP_OF = {
    "BTCUSD": "crypto_beta",
    "ETHUSD": "crypto_beta",
    "XAUTUSD": "gold",
    "PAXGUSD": "gold",
}


class RiskLimits(BaseModel):
    """Typed, validated limits. All *_pct values are fractions of equity
    (0.03 == 3%). Config file: config/risk_limits.yaml (versioned in git)."""

    margin_cap_pct: float = Field(gt=0, le=1.0, default=0.5)
    max_lots_per_underlying: int = Field(gt=0, default=4)
    max_notional_usd_per_underlying: float = Field(gt=0, default=2000.0)
    max_group_notional_usd: Mapping[str, float] = Field(
        default_factory=lambda: {"crypto_beta": 3000.0, "gold": 2000.0}
    )
    max_net_delta_usd: float = Field(gt=0, default=1500.0)
    max_spread_pct: Mapping[str, float] = Field(
        default_factory=lambda: {"default": 0.05, "XAUT*": 0.35}
    )
    max_open_book_loss_pct: float = Field(gt=0, le=1.0, default=0.10)
    max_daily_loss_pct: float = Field(gt=0, le=1.0, default=0.05)
    max_weekly_loss_pct: float = Field(gt=0, le=2.0, default=0.10)
    max_drawdown_pct: float = Field(gt=0, le=1.0, default=0.20)  # breaker
    stale_data_s: float = Field(gt=0, default=15.0)
    min_bid_size: int = Field(gt=0, default=1)
    max_open_orders: int = Field(gt=0, default=10)
    # validated Europa-style: a typo/missing field is a load error, not a default


@dataclass
class RiskEvent:
    kind: str  # halt | reset | breach_note
    ts: str
    detail: str


@dataclass
class PortfolioRiskEngine:
    """Stateful ONLY in PnL counters + the halt flag; checks are pure."""

    limits: RiskLimits
    halted: bool = False
    halt_reason: str = ""
    events: list[RiskEvent] = field(default_factory=list)

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def group_of(symbol: str) -> str:
        return GROUP_OF.get(symbol, "other")

    def spread_cap(self, symbol: str) -> float:
        for pat, cap in self.limits.max_spread_pct.items():
            if pat.endswith("*") and symbol.startswith(pat[:-1]):
                return cap
        return float(self.limits.max_spread_pct.get("default", 0.05))

    def note(self, ts: str, kind: str, detail: str) -> None:
        self.events.append(RiskEvent(kind=kind, ts=ts, detail=detail))

    # ---- exposure ---------------------------------------------------------
    def exposure(
        self,
        positions: list[PositionSnapshot],
        contract_value_usd: Mapping[str, float],
    ) -> dict[str, float]:
        """{'net_delta_usd', 'group:<g>', 'underlying:<sym>'} in USD."""
        out: dict[str, float] = {"net_delta_usd": 0.0}
        for p in positions:
            net = p.net_size
            if net == 0:
                continue
            evd = abs(net) * float(contract_value_usd.get(p.symbol, 0.0))
            k_u, k_g = f"underlying:{p.symbol}", f"group:{self.group_of(p.symbol)}"
            out[k_u] = out.get(k_u, 0.0) + evd
            out[k_g] = out.get(k_g, 0.0) + evd
            out["net_delta_usd"] += net * float(contract_value_usd.get(p.symbol, 0.0))
        return out

    @staticmethod
    def open_book_pnl(positions: list[PositionSnapshot]) -> Decimal:
        """Unrealized (MTM) P&L across the book — sum of exchange marks."""
        total = ZERO
        for p in positions:
            total += p.unrealized_pnl or ZERO
        return total

    # ---- circuit breaker --------------------------------------------------
    def check_drawdown(
        self, now_s: str, equity: Decimal, start_equity: Decimal
    ) -> None:
        """Portfolio drawdown breaker: flatten + halt when breached."""
        if self.halted or start_equity <= ZERO:
            return
        dd = (start_equity - equity) / start_equity
        if float(dd) >= self.limits.max_drawdown_pct:
            self._set_halt(
                now_s,
                f"equity drawdown {float(dd):.2%} >= "
                f"{self.limits.max_drawdown_pct:.2%}",
            )

    def check_mtm_loss(
        self, now_s: str, equity: Decimal, positions: list[PositionSnapshot]
    ) -> None:
        open_loss = -self.open_book_pnl(positions)  # positive when losing
        if float(open_loss) > self.limits.max_open_book_loss_pct * float(equity):
            self._set_halt(
                now_s,
                f"open book loss {open_loss} > "
                f"{self.limits.max_open_book_loss_pct:.0%} of equity",
            )

    def check_pnl_limits(
        self, now_s: str, day_pnl: Decimal, weekly_pnl: Decimal
    ) -> None:
        if float(day_pnl) <= -self.limits.max_daily_loss_pct:
            self._set_halt(now_s, f"daily loss {day_pnl}")
        if float(weekly_pnl) <= -self.limits.max_weekly_loss_pct:
            self._set_halt(now_s, f"weekly loss {weekly_pnl}")

    def _set_halt(self, ts: str, reason: str) -> None:
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        self.note(ts, "halt", reason)

    def reset_halt(self, ts: str, operator: str = "operator") -> None:
        """Operator-only: enables trading again (must ALSO have flattened)."""
        self.halted = False
        self.halt_reason = ""
        self.note(ts, "reset", f"halt reset by {operator}")

    # ---- pre-trade gate ---------------------------------------------------
    def pre_trade(
        self,
        intent: OrderIntent,
        acct: AccountSnapshot,
        ticker: Ticker | None,
        positions: list[PositionSnapshot],
        contract_value_usd: float,
        bid_size: int | None = None,
        equity: Decimal | None = None,
        open_orders_count: int = 0,
    ) -> tuple[bool, list[str]]:
        """Veto gate before any order is sent. Returns (ok, reasons)."""
        reasons: list[str] = []
        eq = equity if equity is not None else acct.equity

        if self.halted:
            reasons.append("halted_by_circuit_breaker")
        if intent.size <= 0 or intent.size != int(intent.size):
            reasons.append("size_must_be_positive_int")
        if intent.limit_price is not None and intent.limit_price <= ZERO:
            reasons.append("non_positive_limit_price")
        if acct.available_balance <= ZERO:
            reasons.append("no_available_balance")
        if used_pct(acct) > self.limits.margin_cap_pct:
            reasons.append("margin_over_cap")

        add = abs(intent.size) * contract_value_usd
        exp = self.exposure(positions, {intent.product_symbol: contract_value_usd})
        group = self.group_of(intent.product_symbol)
        if (
            abs(
                exp["net_delta_usd"] + signed(intent) * intent.size * contract_value_usd
            )
            > self.limits.max_net_delta_usd
        ):
            reasons.append("net_delta_cap")
        if exp.get(
            f"group:{group}", 0.0
        ) + add > self.limits.max_group_notional_usd.get(group, float("inf")):
            reasons.append("group_notional_cap")
        if (
            exp.get(f"underlying:{intent.product_symbol}", 0.0) + add
            > self.limits.max_notional_usd_per_underlying
        ):
            reasons.append("underlying_notional_cap")

        if ticker is not None:
            sp = ticker.spread_pct
            if sp is not None and float(sp) > self.spread_cap(intent.product_symbol):
                reasons.append(
                    f"spread_wide({float(sp):.3f}>"
                    f"{self.spread_cap(intent.product_symbol):.3f})"
                )
        if bid_size is not None and bid_size < self.limits.min_bid_size:
            reasons.append("thin_top_of_book")
        if open_orders_count >= self.limits.max_open_orders:
            reasons.append("too_many_open_orders")
        if eq <= ZERO:
            reasons.append("equity_non_positive")
        return (not reasons, reasons)


def signed(intent: OrderIntent) -> int:
    return 1 if intent.side.value == "buy" else -1


def used_pct(acct: AccountSnapshot) -> float:
    if not acct.wallet_balance or acct.wallet_balance <= ZERO:
        return 0.0
    return float(acct.used_margin / acct.wallet_balance)
