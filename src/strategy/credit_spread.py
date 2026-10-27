from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.fees import option_fee
from core.models import (
    Candle,
    FillEvent,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    PositionSnapshot,
    Side,
    Ticker,
)
from core.oms import OrderBook
from core.options import CreditSpread, SpreadLeg
from core.portfolio_risk import PortfolioRiskEngine, RiskLimits
from core.ports import ExecutionPort

ZERO = Decimal("0")


class SpreadCfg(BaseModel):
    """Typed, validated config (file: config/credit_spread.yaml). PAPER only
    until every gate in scripts/golive_check.py passes AND an operator sets
    the explicit live flag."""

    underlyings: list[str] = Field(default_factory=lambda: ["BTCUSD", "ETHUSD"])
    short_delta_low: float = Field(ge=0, le=1, default=0.10)
    short_delta_high: float = Field(ge=0, le=1, default=0.30)
    width_strikes: int = Field(gt=0, default=2)
    max_spread_pct: float = Field(gt=0, default=0.05)  # XAUT-only override
    min_credit_over_fee: float = Field(gt=0, default=2.0)
    max_open_spreads_per_underlying: int = Field(gt=0, default=2)
    stop_loss_credit_multiple: float = Field(gt=0, default=2.0)
    notional_budget_usd: float = Field(gt=0, default=400.0)
    sell_premium_slip_bps: int = Field(gt=0, default=50)
    buy_premium_slip_bps: int = Field(gt=0, default=50)
    allowed_kinds: tuple[str, ...] = ("call", "put")

    def _validate(self) -> None:
        if self.short_delta_high <= self.short_delta_low:
            raise ValueError("delta band empty")
        if any("XAUT" in u for u in self.underlyings) and self.max_spread_pct < 0.10:
            raise ValueError(
                "XAUT books quote ~35% wide; max_spread_pct "
                "must reflect that or XAUT will never clear"
            )
        if self.stop_loss_credit_multiple <= 0:
            raise ValueError("stop must be positive")


@dataclass(frozen=True)
class Quote:
    """One tradable option leg (already liquidity-checked by the feed)."""

    symbol: str  # C-BTC-79500-301026 grammar
    underlying: str  # BTC | ETH | XAUT
    kind: str  # call | put
    strike: Decimal
    expiry: str  # YYYY-MM-DD
    delta: float  # greeks.delta of a buyer of that leg
    best_bid: Decimal
    best_ask: Decimal
    bid_size: int
    contract_value: Decimal  # coins per contract
    underlying_price: Decimal  # index level (fee-notional basis)


@dataclass(frozen=True)
class OpenSpread:
    spread: CreditSpread
    underlying: str
    opened_ts: str
    entry_credit_usd: Decimal
    mark_cost_usd: Decimal  # current buy-back value of book (>=0) MTM
    symbol: str = ""


@dataclass(frozen=True)
class MarketState:
    now: str  # "2026-10-22T09:30:00Z" — UTC everywhere
    quotes: Sequence[Quote]
    open_spreads: Sequence[OpenSpread] = ()


@dataclass
class Decision:
    open_orders: list[OrderIntent] = field(default_factory=list)
    stop_outs: list[str] = field(default_factory=list)  # symbols to flatten
    rejects: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# pure core
# ---------------------------------------------------------------------------
def _fee_fn() -> Callable[[Decimal, Decimal, Decimal, int], Decimal]:
    from core.fees import option_fee

    return option_fee


def stop_outs(state: MarketState, cfg: SpreadCfg) -> list[str]:
    """Close rule: MTM cost >= cfg.stop_loss_credit_multiple x entry credit.
    Returns the OpenSpread symbols to flatten (reduce-only)."""
    out: list[str] = []
    for os_ in state.open_spreads:
        stop = os_.entry_credit_usd * Decimal(str(cfg.stop_loss_credit_multiple))
        if os_.mark_cost_usd >= stop:
            out.append(os_.symbol)
    return out


def evaluate(
    state: MarketState,
    cfg: SpreadCfg,
    option_fee: Callable[[Decimal, Decimal, Decimal, int], Decimal] | None = None,
) -> Decision:
    """(market state, positions, config) -> target orders. No I/O."""
    cfg._validate()
    dec = Decision()
    dec.stop_outs = stop_outs(state, cfg)
    fee_fn = option_fee or _fee_fn()

    by_u: dict[str, dict[str, list[Quote]]] = {}
    for q in state.quotes:
        by_u.setdefault(q.underlying, {}).setdefault(q.kind, []).append(q)

    for underlying, kinds in by_u.items():
        n_open = sum(1 for os_ in state.open_spreads if os_.underlying == underlying)
        if n_open >= cfg.max_open_spreads_per_underlying:
            dec.rejects.append(f"max_open:{underlying}")
            continue
        for kind in cfg.allowed_kinds:
            legs = sorted(kinds.get(kind, []), key=lambda v: v.strike)
            band_mid = (cfg.short_delta_low + cfg.short_delta_high) / 2.0
            shorts = [
                q
                for q in legs
                if cfg.short_delta_low <= abs(q.delta) <= cfg.short_delta_high
            ]
            if not shorts:
                dec.rejects.append(f"no_leg_in_band:{underlying}:{kind}")
                continue
            # deterministic: the in-band leg whose |delta| is nearest band mid
            short_q = min(shorts, key=lambda v: abs(abs(v.delta) - band_mid))
            longs = [
                q
                for q in legs
                if (kind == "call" and q.strike > short_q.strike)
                or (kind == "put" and q.strike < short_q.strike)
            ]
            if not longs:
                dec.rejects.append(f"no_long_leg:{underlying}:{kind}")
                continue
            long_q = longs[min(cfg.width_strikes, len(longs)) - 1]
            if short_q.bid_size < 1 or short_q.best_bid <= ZERO:
                dec.rejects.append(f"thin_book:{short_q.symbol}")
                continue
            if (
                short_q.best_ask == ZERO
                or long_q.best_ask == ZERO
                or long_q.best_bid == ZERO
            ):
                dec.rejects.append(f"dead_quote:{long_q.symbol}")
                continue
            mid = (short_q.best_bid + short_q.best_ask) / 2
            if mid > ZERO and (short_q.best_ask - short_q.best_bid) / mid > Decimal(
                str(cfg.max_spread_pct)
            ):
                dec.rejects.append(f"spread_wide:{short_q.symbol}")
                continue
            sell_prem = short_q.best_bid * (
                Decimal("1") - Decimal(cfg.sell_premium_slip_bps) / Decimal("10000")
            )
            buy_prem = long_q.best_ask * (
                Decimal("1") + Decimal(cfg.buy_premium_slip_bps) / Decimal("10000")
            )
            sp = CreditSpread(
                kind=kind,
                short=SpreadLeg(kind, "sell", short_q.strike, sell_prem),
                long=SpreadLeg(kind, "buy", long_q.strike, buy_prem),
                expiry=short_q.expiry,
            )
            fee_est = fee_fn(
                short_q.underlying_price, short_q.best_bid, short_q.contract_value, 1
            ) + fee_fn(
                long_q.underlying_price, long_q.best_ask, long_q.contract_value, 1
            )
            credit = sp.net_credit_per_coin() * short_q.contract_value
            if credit <= ZERO:
                dec.rejects.append(f"no_net_credit:{short_q.symbol}")
                continue
            min_credit = fee_est * Decimal(str(cfg.min_credit_over_fee))
            if credit < min_credit or sp.max_loss_per_coin() <= ZERO:
                dec.rejects.append(f"credit_too_thin:{short_q.symbol}")
                continue
            budget = Decimal(str(cfg.notional_budget_usd))
            one_loss = sp.max_loss_usd(short_q.contract_value)
            size = max(1, min(int(budget / one_loss) if one_loss else 1, 100))
            dec.open_orders.append(
                OrderIntent(
                    algo="credit_spread",
                    product_symbol=short_q.symbol,
                    side=Side.SELL,
                    size=size,
                    limit_price=sell_prem.quantize(Decimal("0.0001")),
                    purpose="spread_short",
                    reason=f"credit_spread entry {kind} {underlying} "
                    f"delta_band=({cfg.short_delta_low},{cfg.short_delta_high})",
                    meta={
                        "paired_long_leg": long_q.symbol,
                        "expiry": short_q.expiry,
                        "max_loss_usd": str(
                            sp.max_loss_usd(short_q.contract_value) * size
                        ),
                        "credit_usd_est": str(credit * size),
                    },
                )
            )
            dec.open_orders.append(
                OrderIntent(
                    algo="credit_spread",
                    product_symbol=long_q.symbol,
                    side=Side.BUY,
                    size=size,
                    limit_price=buy_prem.quantize(Decimal("0.0001")),
                    purpose="spread_hedge_long",
                    reason="defined-risk cap for spread_short",
                    meta={"paired_short_leg": short_q.symbol},
                )
            )
    return dec


class CreditSpreadStrategy:
    """Plugin adapter: StrategyPort-compatible wrapper PURE core API.

    The ambitious parts (fires nothing, no bookkeeping) belong to the run
    harness; this object only frames evaluate() as the plugin interface.
    """

    NAME = "credit_spread"

    def __init__(self, cfg: SpreadCfg) -> None:
        self.cfg = cfg

    def on_market_state(self, state: MarketState) -> Decision:
        return evaluate(state, self.cfg)


# ---------------------------------------------------------------------------
# runner adapter (the I/O half) — implements StrategyPort against a port
# ---------------------------------------------------------------------------

log = logging.getLogger("fm.credit_spread")

_RISK_LIMITS_PATH = Path(__file__).resolve().parents[2] / "config" / "risk_limits.yaml"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _parse_option_symbol(
    sym: str,
) -> tuple[str, str, Decimal, str] | None:
    """C-BTC-79600-251026 -> (BTC, "call", 79600, "2026-10-25") or None.

    Grammar: {C|P}-{UNDERLYING}-{STRIKE}-{DDMMYY}; dailies expire 12:00 UTC.
    """
    parts = sym.split("-")
    if len(parts) != 4 or parts[0] not in ("C", "P"):
        return None
    if not parts[2].replace(".", "", 1).isdigit():
        return None
    raw = parts[3]
    if not raw.isdigit() or len(raw) != 6:
        return None
    expiry = "20" + raw[4:6] + "-" + raw[2:4] + "-" + raw[:2]  # DDMMYY
    return parts[1], ("call" if parts[0] == "C" else "put"), Decimal(parts[2]), expiry


class CreditSpreadRunner:
    """StrategyPort adapter: feeds real tickers into the PURE core, applies
    the PortfolioRiskEngine pre-trade veto, sends order intents and keeps
    open-spread bookkeeping synced FROM THE PORT (positions are truth).

    The same object runs on BacktestPort, PaperPort and (post-gates) the
    live port — spec 3.2 one-strategy-many-ports.
    """

    name = "credit_spread"
    algo = "credit_spread"

    def __init__(self, cfg: SpreadCfg) -> None:
        self.cfg = cfg
        self.port: ExecutionPort | None = None
        self.risk = PortfolioRiskEngine(limits=RiskLimits())
        self.book = OrderBook()
        self.quotes: dict[str, Quote] = {}
        self.spots: dict[str, Decimal] = {}
        self.open_spreads: list[OpenSpread] = []
        self._option_syms: dict[str, list[str]] = {}
        # short_symbol -> {"spread": CreditSpread, "entry_credit_usd": ..,
        #                  "size": int, "paired_long_leg": sym}
        self._pairs: dict[str, dict[str, Any]] = {}
        self._halts_reported = 0

    # -- bootstrap ----------------------------------------------------------
    async def bootstrap(self, exec_port: ExecutionPort) -> None:
        self.port = exec_port
        try:
            import yaml

            data = yaml.safe_load(_RISK_LIMITS_PATH.read_text())
            self.risk = PortfolioRiskEngine(limits=RiskLimits(**data))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "risk_limits.yaml missing/invalid (%s) — validated defaults in use", exc
            )
        products = await self.port.products()
        for u in self.cfg.underlyings:
            base = u[:3] if u[:3] in ("BTC", "ETH", "XAU") else u
            base_sym = base + "USD"
            self._option_syms[base_sym] = [
                p.symbol
                for p in products
                if p.strike_price is not None
                and (p.underlying_asset_symbol or "") == base
                and _parse_option_symbol(p.symbol) is not None
            ]
        log.info(
            "credit_spread runner up: option universe=%s risk_limits=%s",
            {k: len(v) for k, v in self._option_syms.items()},
            self.risk.limits.model_dump(),
        )

    async def _sync_open_spreads(self) -> None:
        """The port is the truth: pairs whose short position is flat are gone;
        open ones get fresh MTM marks (buy-back cost)."""
        assert self.port is not None
        alive: list[OpenSpread] = []
        for short_sym, meta in list(self._pairs.items()):
            pos: PositionSnapshot | None
            try:
                pos = await self.port.position(short_sym)
            except Exception:  # noqa: BLE001
                pos = None
            net = pos.net_size if pos else 0
            if not net:
                del self._pairs[short_sym]
                continue
            s_q = self.quotes.get(short_sym)
            l_q = self.quotes.get(str(meta.get("paired_long_leg")))
            mark = ZERO
            if s_q is not None and l_q is not None:
                mark = max(
                    (s_q.best_ask - l_q.best_bid)
                    * s_q.contract_value
                    * int(meta["size"]),
                    ZERO,
                )
            alive.append(
                OpenSpread(
                    spread=meta["spread"],
                    underlying=str(meta["underlying"]),
                    opened_ts=str(meta.get("opened_ts", _today())),
                    entry_credit_usd=Decimal(str(meta["entry_credit_usd"])),
                    mark_cost_usd=mark,
                    symbol=short_sym,
                )
            )
        self.open_spreads = alive

    async def _collect_quotes(self) -> None:
        """Refresh quoted candidates: near-spot, near-expiry, alive books."""
        assert self.port is not None
        today = _today()
        for base_sym, syms in self._option_syms.items():
            spot = self.spots.get(base_sym)
            if spot is None or spot <= ZERO:
                continue
            lo = spot * Decimal("0.75")
            hi = spot * Decimal("1.25")
            fresh: list[Quote] = [
                q for q in self.quotes.values() if q.underlying + "USD" == base_sym
            ]
            for sym in syms:
                parsed = _parse_option_symbol(sym)
                if not parsed:
                    continue
                _, kind, strike, expiry = parsed
                if not (lo <= strike <= hi):
                    continue
                dte = (
                    datetime.strptime(expiry, "%Y-%m-%d")
                    - datetime.strptime(today, "%Y-%m-%d")
                ).days
                if dte < 0 or dte > 2:
                    continue
                t = await self.port.ticker(sym)
                if t is None or t.best_bid is None or t.best_ask is None:
                    continue
                if t.best_bid <= ZERO or t.best_ask <= ZERO:
                    continue
                p = await self.port.product(sym)
                size_quote = int((t.raw.get("quotes") or {}).get("bid_size", 1) or 1)
                fresh.append(
                    Quote(
                        symbol=sym,
                        underlying=base_sym[:-3],
                        kind=kind,
                        strike=strike,
                        expiry=expiry,
                        delta=float(t.delta or 0),
                        best_bid=t.best_bid,
                        best_ask=t.best_ask,
                        bid_size=max(size_quote, 1),
                        contract_value=p.contract_value if p else Decimal("0.001"),
                        underlying_price=spot,
                    )
                )
            self.quotes = {q.symbol: q for q in fresh}

    # -- StrategyPort -------------------------------------------------------
    async def on_candle_close(self, candle: Candle) -> None:
        if (
            candle.symbol.endswith("USD")
            and _parse_option_symbol(candle.symbol) is None
        ):
            self.spots[candle.symbol] = Decimal(str(candle.close))

    async def on_ticker(self, symbol: str, ticker: Ticker) -> None:
        if ticker.spot_price and symbol.endswith("USD"):
            self.spots[symbol] = ticker.spot_price

    async def on_fill(self, fill: FillEvent) -> None:
        rec = self.book.orders.get(fill.client_order_id)
        if rec is not None:
            try:
                self.book.apply_fill(fill)
            except Exception:  # noqa: BLE001
                self._report_halt("apply_fill", [fill.client_order_id])

    async def on_timer(self) -> None:
        """Heartbeat: reconcile -> marks -> stops (reduce-only) -> entries."""
        if self.port is None:
            return
        try:
            await self._reconcile_book()
            await self._collect_quotes()
            await self._sync_open_spreads()
        except Exception:  # noqa: BLE001
            log.exception("timer preflight failed")
            return
        state = MarketState(
            now=_today(),
            quotes=list(self.quotes.values()),
            open_spreads=list(self.open_spreads),
        )
        dec = evaluate(state, self.cfg, option_fee=option_fee)
        acct = await self.port.account()
        # 1) risk stops — reduce-only, no growth veto
        for sym in dec.stop_outs:
            meta = self._pairs.get(sym)
            if not meta:
                continue
            long_sym = str(meta["paired_long_leg"])
            short_leg_side = Side.BUY  # buy back the short
            self._pairs.pop(sym, None)
            for leg, side in ((sym, short_leg_side), (long_sym, Side.SELL)):
                await self._send(
                    OrderIntent(
                        algo=self.algo,
                        product_symbol=leg,
                        side=side,
                        size=int(meta["size"]),
                        purpose="spread_stop",
                        reduce_only=True,
                        reason=f"stop_out {sym} (2x credit multiples of MTM)",
                    ),
                    acct,
                    veto=False,
                )
        # 2) entries
        await self._enter_pairs(dec, acct)
        for r in dec.rejects:
            log.debug("credit_spread skip: %s", r)
        self._flush_mismatch_journal()

    async def _enter_pairs(self, dec: Decision, acct: Any) -> None:
        shorts = [o for o in dec.open_orders if o.side is Side.SELL]
        longs = {o.product_symbol: o for o in dec.open_orders if o.side is Side.BUY}
        for s in shorts:
            long_sym = str(s.meta.get("paired_long_leg", ""))
            lo = longs.get(long_sym)
            s_q, l_q = self.quotes.get(s.product_symbol), self.quotes.get(long_sym)
            if lo is None or s_q is None or l_q is None:
                continue
            sp = CreditSpread(
                kind=s_q.kind,
                short=SpreadLeg(
                    s_q.kind, "sell", s_q.strike, s.limit_price or s_q.best_bid
                ),
                long=SpreadLeg(
                    l_q.kind, "buy", l_q.strike, lo.limit_price or l_q.best_ask
                ),
                expiry=s_q.expiry,
            )
            credit_usd = sp.net_credit_usd(s_q.contract_value) * s.size
            ok_s = await self._send(s, acct, veto=True)
            ok_l = await self._send(lo, acct, veto=True)
            if ok_s and ok_l:
                self._pairs[s.product_symbol] = {
                    "spread": sp,
                    "entry_credit_usd": abs(credit_usd),
                    "size": s.size,
                    "paired_long_leg": long_sym,
                    "underlying": s_q.underlying,
                    "opened_ts": _today(),
                }

    async def _send(self, intent: OrderIntent, acct: Any, veto: bool) -> bool:
        """Place through the port with the risk veto. Returns accepted."""
        assert self.port is not None
        if not intent.client_order_id:
            intent = intent.with_client_id(
                self.algo, intent.product_symbol, intent.purpose
            )
        if veto:
            q = self.quotes.get(intent.product_symbol)
            p = await self.port.product(intent.product_symbol)
            if q is None or p is None:
                return False
            cv_usd = float(p.contract_value) * float(q.underlying_price)
            ok, why = self.risk.pre_trade(
                intent,
                acct,
                Ticker(
                    symbol=intent.product_symbol,
                    best_bid=q.best_bid,
                    best_ask=q.best_ask,
                ),
                [],  # option legs carry no futures position here
                contract_value_usd=cv_usd,
                bid_size=int(q.bid_size),
                equity=acct.equity,
                open_orders_count=len(self.book.orders),
            )
            if not ok:
                log.info("risk veto %s: %s", intent.product_symbol, why)
                return False
        try:
            res = await self.port.place(intent)
        except Exception:  # noqa: BLE001
            log.exception("place failed for %s", intent.product_symbol)
            return False
        self.book.submit(OrderRecordFactory(intent))
        return bool(res and getattr(res, "accepted", False))

    async def _reconcile_book(self) -> None:
        assert self.port is not None
        open_orders = await self.port.open_orders()
        ex = [
            {
                "client_order_id": o.client_order_id,
                "symbol": o.symbol,
                "side": str(o.side.value),
                "size": o.size,
                "status": str(getattr(o, "status", "open")),
                "filled_size": o.filled_size,
            }
            for o in open_orders
            if isinstance(getattr(o, "client_order_id", ""), str) and o.client_order_id
        ]
        mm = self.book.reconcile(ex)
        if any(m.startswith("halt:") for m in mm):
            self._report_halt("reconcile", mm)

    def _report_halt(self, where: str, msgs: list[str]) -> None:
        self._halts_reported += 1
        log.error("HALT event (%s): %s", where, msgs)
        try:
            p = Path("state/soak_mismatches.json")
            j = {"unresolved": self._halts_reported}
            p.parent.mkdir(exist_ok=True)
            p.write_text(json_dump(j))
        except OSError:
            pass

    def _flush_mismatch_journal(self) -> None:
        pass  # journal written on halts only (gate 3 counts unresolved)

    def status(self) -> dict[str, Any]:
        return {
            "open_spreads": len(self.open_spreads),
            "pairs": sorted(self._pairs.keys()),
            "tracked_quotes": len(self.quotes),
            "spots": {k: str(v) for k, v in self.spots.items()},
            "halts": list(self.book.halts)[-10:],
            "unresolved": self._halts_reported,
        }


# --- tiny helpers -----------------------------------------------------------
def OrderRecordFactory(intent: OrderIntent) -> OrderRecord:
    """Light local-order mirror for idempotency (OMS submit)."""
    return OrderRecord(
        client_order_id=intent.client_order_id,
        symbol=intent.product_symbol,
        side=intent.side,
        size=intent.size,
        status=OrderStatus.NEW,
    )


def json_dump(obj: Any) -> str:
    import json

    return json.dumps(obj, indent=1)
