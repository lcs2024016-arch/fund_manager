# ARCHITECTURE — risk-first trading bot (M1 core+paper, M2 execution, M3 CI)

## Design principles
1. **One strategy core, three execution worlds.** The pure module
   `src/strategy/credit_spread.py::evaluate()` maps
   `(MarketState, SpreadCfg) -> Decision` with **no I/O**. The backtester
   (`src/backtest/engine.py`), the paper simulator (`src/paper/port.py`) and
   any future live port call the same function; only the market-data feed and
   the ExecutionPort differ. This is what makes the parity test meaningful
   (`tests/test_parity_paper_backtest.py`).
2. **Risk is code, not convention.** `src/core/portfolio_risk.py` owns the
   veto gate every order must pass; `src/core/oms.py` owns the order state
   machine and reconciliation; `src/core/options.py` refuses structures that
   are not defined-risk.
3. **The exchange is the source of truth.** On start and on every reconnect,
   the local book is rebuilt from REST/WS state (`OrderBook.reconcile`);
   unresolvable mismatches append `halt:` events and the runner must flatten
   and stop.
4. **Fail closed.** Paper mode is the default everywhere (Docker CMD, config).
   Live requires `scripts/golive_check.py` exit 0 AND
   `--i-understand-this-is-live`. Tests assert this (`test_golive_gates.py`).

## Module map (new in M1–M3)
| Module | Responsibility | Tested by |
|---|---|---|
| `core/conversions.py` | per-coin premium ↔ USD, contract sizing; guards the 1000x bug class | `test_core_units.py` |
| `core/fees.py` | VERIFIED option fee `min(1bp·notional, 3.5%·premium) × 1.18 GST`; UNVERIFIED linear defaults flagged | `test_core_units.py` |
| `core/options.py` | payoff, settlement PnL, `CreditSpread` defined-risk validation & max-loss math | `test_core_units.py` |
| `core/portfolio_risk.py` | `RiskLimits` (pydantic), exposure aggregation incl. BTC+ETH group cap, MTM open-book limit, daily/weekly loss, drawdown breaker (flatten+halt), pre-trade veto | `test_portfolio_risk.py`, property tests |
| `core/oms.py` | order states (new→open→partial→filled/cancelled/rejected/unknown), idempotent submit, overfill/unknown-fill detection, exchange-trusted reconcile | `test_oms.py`, `test_fake_exchange.py`, `test_chaos_restart.py` |
| `strategy/credit_spread.py` | pure strategy: delta-band short verticals, slippage-guarded pricing, credit≥2×fee, stop at `stop_loss_credit_multiple×credit`, per-underlying open cap; deterministic | `test_credit_spread.py`, property tests |
| `scripts/golive_check.py` | the 6 go-live gates, prints which gate failed and why, exit 1 otherwise | `test_golive_gates.py` |
| `scripts/walkforward_eval.py` | weekly OOS re-run + deflated-Sharpe haircut + config log (gate 2 evidence) | run manually/scheduled |
| `scripts/collector_health.py` | nightly collector heartbeat | scheduled workflow |

## Reused (pre-existing, wired in)
- `src/delta/*` — signed REST client, products catalog, rate limiter, signer
  (spec 1.2 HMAC). No `/v2` prefix on repo client paths (`/products`,
  `/tickers/{symbol}`) — matches `docs/API_NOTES.md`.
- `src/paper/port.py` — pessimistic fills (buy at ask, sell at bid),
  persisted state; mocked-REST driven in tests via `httpx.MockTransport` +
  the recorded fixture `tests/fixtures/products_btcusd.json` (real
  `delta_products.json` record).
- `src/backtest/engine.py` — candle replay, higher-TF bucket closes,
  market-fill with 1 bp slippage.
- `src/core/ledger.py` / `core/state.py` — SQLite state.
- `src/core/config.py` — `[ASK]` null-value live gating, `Config.flags`.

## Data flow (paper)
```
Delta REST/WS ─► PaperPort (ticker/candles) ─► MarketState
                                                   │  (pure)
                                              evaluate()
                                                   ▼
                                       Decision{open_orders, stop_outs, rejects}
                                                   │
                                       PortfolioRiskEngine.pre_trade()  ◄─ AccountSnapshot
                                                   ▼ (veto)
                                              PaperPort.place()  → FillEvent
                                                   ▼
                                       OrderBook.apply_fill / reconcile
```

## June-2026 tail policy (explicit)
The strategy keeps the leader's premium-capture component but caps each
structure with a long leg. `tests/test_june2026_tail_reproduces_...` asserts,
numerically, that (a) a −12% BTC week still produces the capped worst-case
loss (`width − credit`), and (b) at oversize this loss exceeds the leader's
OBSERVED June-26 giveback (−$347 per $1,000 margin) while at BTC-real
contract scaling it is a small fraction. The portfolio MTM breaker additionally
halts at −10% open-book equity BEFORE the full June bleed is reached
(`test_june2026_tail_flagged_not_hidden`). Nothing is hidden; the bleed is
represented and bounded.

## What is deliberately out of architecture scope (M4/M5)
- Live ExecutionPort hardening and alert transports (Telegram/email) —
  M4/M5; the port interface (`ExecutionPort`) already exists.
- WebSocket order-channel auto-recover loop in the runner (the ws module
  exists; the credit-spread runner drives REST polling in paper for now).
