"""Golden backtest regression: fixed seed/pinned data; CI fails if key
metrics move beyond tolerances without a deliberate, reviewed golden-file
update (delete-but-review workflow: update file + mention in CHANGELOG)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
from decimal import Decimal
from pathlib import Path

import pytest

from core.models import Candle, ProductInfo

GOLDEN = Path(__file__).parent / "golden_backtest.json"
TOKEN_TOL = {
    "final_equity": 1e-6,  # relative
    "fills": 0,  # exact
    "worst_dd": 0.01,  # relative
}
TAKER = "0.0007"
MAKER = "0.0002"


def _candles():
    from datetime import datetime, timedelta

    ts = datetime(2024, 1, 1, tzinfo=UTC)
    price = 100.0
    out = []
    for i in range(200):
        price += 0.5 if i % 2 == 0 else -0.3  # deterministic, no RNG drift
        out.append(
            Candle(
                symbol="TOYUSD",
                resolution="1m",
                ts=ts + timedelta(minutes=i),
                open=price,
                high=price * 1.001,
                low=price * 0.999,
                close=price,
                volume=10.0,
                closed=True,
            )
        )
    return out


class MomentumQuick:
    """Deterministic toy strategy (same shape as tests/test_backtest_engine)."""

    in_position = False

    async def bootstrap(self, exec_port) -> None:
        self.exec = exec_port

    async def on_candle_close(self, candle) -> None:
        from core.models import OrderIntent, OrderType, Side

        want = getattr(self, "last_close", candle.close) < candle.close
        self.last_close = candle.close
        if want and not self.in_position:
            self.in_position = True
            await self.exec.place(
                OrderIntent(
                    algo="golden",
                    product_symbol="TOYUSD",
                    side=Side.BUY,
                    size=1,
                    order_type=OrderType.MARKET,
                    reason="mom_up",
                )
            )
        elif not want and self.in_position:
            self.in_position = False
            await self.exec.place(
                OrderIntent(
                    algo="golden",
                    product_symbol="TOYUSD",
                    side=Side.SELL,
                    size=1,
                    order_type=OrderType.MARKET,
                    reduce_only=True,
                    reason="mom_down",
                )
            )

    async def on_ticker(self, *a) -> None:
        pass

    async def on_fill(self, *a) -> None:
        pass

    async def on_timer(self) -> None:
        pass

    def status(self) -> dict:
        return {}


def _metrics(summary) -> dict:
    curve = summary["equity_curve"]
    eq = max((e["equity"] for e in curve), default=0.0)
    worst = min((e["equity"] for e in curve), default=0.0)
    return {
        "final_equity": summary["final_equity"],
        "initial_equity": summary["initial_equity"],
        "fills": summary["fills"],
        "worst_dd": summary["final_equity"] - worst,
        "peak": eq,
    }


def test_golden_backtest_regression(tmp_path):
    from backtest.engine import BacktestPort, run_backtest

    products = {
        "TOYUSD": ProductInfo(
            symbol="TOYUSD",
            product_id=1,
            contract_type="perpetual_futures",
            tick_size=Decimal("0.1"),
            contract_value=Decimal("1"),
            maker_commission_rate=Decimal(MAKER),
            taker_commission_rate=Decimal(TAKER),
        )
    }

    async def run():
        port = BacktestPort(products, Decimal(10000), {})
        return await run_backtest(MomentumQuick(), port, _candles())

    summary = asyncio.run(run())
    metrics = _metrics(summary)
    if not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(metrics, indent=1))
        pytest.skip("golden file generated on first run — review + commit it")
    gold = json.loads(GOLDEN.read_text())
    drift = []
    for k, tol in TOKEN_TOL.items():
        a, b = metrics[k], gold.get(k, metrics[k])
        if isinstance(tol, int):
            if a != b:
                drift.append(f"{k}: {a} != {b}")
        else:
            ref = abs(float(b)) or 1.0
            if abs(float(a) - float(b)) / ref > tol:
                drift.append(f"{k}: {a} vs golden {b} (>{tol:.0%})")
    assert not drift, (
        "GOLDEN REGRESSION — metrics moved beyond tolerance: "
        + "; ".join(drift)
        + ". If intentional, re-run pytest with GOLDEN_UPDATE=1 and commit."
    )


def test_golden_update_command(tmp_path, monkeypatch):
    """Documented escape hatch: GOLDEN_UPDATE=1 regenerates the file."""
    if not GOLDEN.exists():
        pytest.skip("golden file not generated yet")
    from backtest.engine import BacktestPort, run_backtest

    products = {
        "TOYUSD": ProductInfo(
            symbol="TOYUSD",
            product_id=2,
            contract_type="perpetual_futures",
            tick_size=Decimal("0.1"),
            contract_value=Decimal("1"),
            maker_commission_rate=Decimal(MAKER),
            taker_commission_rate=Decimal(TAKER),
        )
    }

    async def run():
        port = BacktestPort(products, Decimal(10000), {})
        return await run_backtest(MomentumQuick(), port, _candles())

    m = _metrics(asyncio.run(run()))
    assert (
        abs(m["final_equity"] - json.loads(GOLDEN.read_text())["final_equity"]) < 1e-6
    )
