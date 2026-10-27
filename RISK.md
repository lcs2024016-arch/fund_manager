# RISK — limits, rationale, and what is NOT modelled

## Context (from validate/REPORT.md — OBSERVED unless stated)
- The Mirrorpip leader recorded **+$1,536.77 all-time realized** on
  naked short-vol option writing, but June 2026 gave back **−$378
  cumulative (−37.8% of margin)**; even at the calendar drawdown trough the
  giveback vs the 12-month figure is **−$347 per $1,000-margin unit**.
- Top-5 days supplied **51% of all-time PnL**; monthly t-stat **1.26**
  (p≈0.23) — the historical edge is **NOT VALIDATED**.
- Replica sizing beyond ~4× margin implies **23% ruin probability**
  (MODELLED, longterm_bt.py).

Consequence: this bot **defaults to PAPER**, keeps premium capture (the only
component with a model-free data trail), but **caps every structure** with a
long option leg and hard portfolio limits.

## Limits (config/risk_limits.yaml — versioned, review-required)
| Limit | Value | Rationale |
|---|---|---|
| `max_drawdown_pct` | 20% | circuit breaker: flatten + halt; operator reset only |
| `max_open_book_loss_pct` | 10% | MTM, not realized-only — halts a June-style bleed before −$347/$1,000 |
| `max_daily_loss_pct` | 5% | one bad day cannot consume the month |
| `max_weekly_loss_pct` | 10% | matches gate-5 drawdown tolerance |
| `max_group_notional_usd` | BTC+ETH 3,000; gold 2,000 | correlated-group cap (crypto_beta, gold) |
| `max_notional_usd_per_underlying` | 2,000 | concentration control |
| `max_net_delta_usd` | ±1,500 | directional exposure band |
| `max_lots_per_underlying` | 4 | no unlimited inventory |
| `max_spread_pct` | 5% default; XAUT 35% | gold option books quote ~35% wide (OBSERVED); BTC/ETH are tight |
| `min_credit_over_fee` | 2× | entry must clear fees+GST with margin (strategy config) |
| `stop_loss_credit_multiple` | 2.0× credit | close before the width is fully at risk |
| `stale_data_s` | 15 s | veto orders on stale quotes |
| `margin_cap_pct` | 50% | headroom for settlement shorts |

## Structure policy
- **Naked option shorts are never built by the strategy.** Only vertical
  credit spreads (short + long same kind, same expiry, long leg
  `width_strikes` outward). Max loss per structure =
  `width − credit` (options.py math, tested in every settle region).
- XAUT is **opt-in only** (`underlyings` list): wide books make entry
  expectancy negative at default caps.
- Sizing: `notional_budget_usd` is the **max-loss** budget, not premium
  spend (`size = budget / max_loss_per_contract`).

## Verified vs UNVERIFIED numbers
- ✅ VERIFIED (collector + docs): option fee = `min(1%×notional /100, 3.5%×premium) × 1.18 GST`.
- ❌ UNVERIFIED: perp/futures maker/taker rates (`core/fees.LinearDefaults`
  marked in code). **Re-read the official fee schedule before any
  money-touching run**; params are configurable without code changes.
- ❌ NOT MODELLED / approximated:
  - settlement price ≈ underlying index at 12:00 UTC expiry (Delta has no
    settle-price endpoint);
  - follower-side slippage beyond the strategy guard (sell `slip_bps` below
    bid, buy above ask);
  - early-assignment/borrow effects (not applicable to cash-margined
    binaries on Delta but unstated);
  - funding for perps in the paper account ledger granularity.

## Gate 5 sign-off (required text, operator fills before live)
> Max tolerable drawdown: ______ USD / ______ % of margin.
> Ruin probability at chosen size: ______% (must be <1%).

Default start: smallest size (`notional_budget_usd: 400`).
