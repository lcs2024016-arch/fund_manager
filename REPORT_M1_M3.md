# MILESTONE REPORT — M1 (core+paper) / M2 (execution+reconciliation) / M3 (CI)

Date: 2026-10-23. All figures below are labeled OBSERVED (real data) or
MODELLED (simulation). No profitability is claimed anywhere.

## What was built

### M1 — pure strategy core + paper
- `src/strategy/credit_spread.py`: pure deterministic core
  `(MarketState, SpreadCfg) -> Decision` — delta-band selected short legs,
  long leg N strikes beyond, sell-below-bid/buy-above-ask slippage guards,
  credit ≥ 2× measured fee (VERIFIED schedule), per-underlying open cap,
  stop-out at `2.0 × entry credit` MTM cost, sizing = budget/max-loss.
  No I/O; the same function drives backtest, paper and (later) live.
- `src/core/options.py`: payoff/settlement/spread math + defined-risk
  validation (refuses naked or wrong-shaped structures).
- `src/core/fees.py`: option fee = `min(1bp×notional, 3.5%×premium) × 1.18
  GST` — VERIFIED from the collector research; linear rates UNVERIFIED and
  flagged in code/config.
- `src/core/conversions.py`: per-coin premium ↔ USD contracts — guards the
  known 1000× bug class with raising unit tests.
- `config/credit_spread.yaml`, `config/risk_limits.yaml`: typed, validated,
  git-versioned configs.

### M2 — risk & execution processes
- `src/core/portfolio_risk.py`: pydantic `RiskLimits`; exposure
  aggregation with per-asset and correlated-group caps (crypto_beta =
  BTC+ETH, gold = XAUT+PAXG); **mark-to-market** open-book loss limit;
  daily and weekly loss limits; drawdown circuit breaker → flatten +
  halt, operator-only reset; `pre_trade()` veto gate (halted state,
  positive integer size, positive limit price, available balance, margin
  cap, net-delta band, group/underlying notional caps, spread cap,
  thin-book, too-many-open-orders, non-positive equity).
- `src/core/oms.py`: explicit order state machine with transition table,
  idempotent submits by client order id, monotonic filled size, overfill
  and unknown-fill halts, and `reconcile()` that TRUSTS THE EXCHANGE
  (status and size adopted, exchange-only orders adopted, local-only open
  orders produce `halt:` mismatches).
- June-2026 tail: explicit tests assert (a) the capped worst-case loss of a
  June-style −12% week equals `width − credit` (MODELLED), (b) oversize
  (2 full-width spreads per $1,000 margin) exceeds the OBSERVED leader
  giveback of −$347 → the sizing gate matters, (c) real BTC contract
  scaling keeps it a small fraction, and (d) the MTM breaker halts at −10%
  of equity — BEFORE the full −34.7% observed bleed. The bleed is
  reproduced and flagged, not hidden.

### M3 — CI/CD
- `.github/workflows/ci.yml`: ruff (lint+format, scoped excludes for
  pre-milestone legacy code — documented in pyproject), `mypy --strict`
  on the 9 risk-critical files (0 errors), gitleaks, dependency audit,
  literal-secret scan, pytest with coverage gate (85% on
  `.coveragerc_gate` scope — actual 92.8%), golden backtest regression,
  fail-closed gate reporting, docker build (non-root, pinned, tagged per
  SHA) + trivy scan.
- `.github/workflows/scheduled.yml`: nightly collector health + backup
  artifact; weekly walk-forward re-evaluation that opens an GitHub issue on
  degradation (config-logged for deflated Sharpe); weekly pip-audit.
- `docker/Dockerfile`: non-root uid 10001, paper-only default CMD (live
  impossible from the container default).
- `.pre-commit-config.yaml` mirrors lint/type/secret/test gates.
- Rollback: previous image SHA re-run + KILL file (RUNBOOK.md).

## Test results (local runs, 2026-10-23)
- `pytest tests -q` → **232 passed** (OBSERVED).
- Coverage gate scope (risk-critical modules, 9 files):
  **92.8 %** ≥ 85 % gate (OBSERVED). Full-repo coverage including legacy
  modules is 69–70 % — legacy modules are pre-milestone and out of the
  M1–M3 gate scope (documented, honest).
- `mypy --strict` on risk-critical files → **0 errors** (OBSERVED).
- `ruff check src scripts tests` → **clean** (OBSERVED; legacy excluded via
  config, listed explicitly).
- Golden backtest: `tests/golden_backtest.json` generated, committed
  (fills=198, final equity 9984.45 MODELLED on a deterministic synthetic
  series — regression-gated, GOLDEN_UPDATE=1 documented escape hatch).
- Go-live gates: **all 6 FAIL, by design, fail-closed** (OBSERVED):
  gate 1 needs ≥8 weeks of collector snapshots (~1 day so far),
  gates 3/4 need real paper/testnet soak weeks, gate 2 needs the
  walk-forward record, gate 5 needs the operator sign-off, gate 6 needs a
  fresh drill record. Time-based, not code-based.

## What is NOT modelled or NOT verified (carry-forward list)
1. Perp/futures maker/taker fee rates — UNVERIFIED
   (`core/fees.LinearDefaults`; verify against the official schedule
   before any money-touching run).
2. Settlement price ≈ underlying index at 12:00 UTC expiry — approximation;
   Delta exposes no settle-price endpoint.
3. Real-match slippage beyond the guards (sell slip below bid, buy above
   ask; paper fill at book edge; backtest market fills at mark ± 1 bp).
   Parity test tolerance documented (≤35 USD display price on 0.001-cv
   BTC ≈ $0.035 of value).
4. WebSocket order-channel auto-recovery in the credit-spread runner —
   REST-poll reconciliation ships now; WS runner is M4/M5.
5. Alert transports (Telegram/email) — interfaces planned for M4/M5;
   RUNBOOK documents manual checks meanwhile.
6. Live-mode runner integration for credit_spread (paper mode only until
   gates pass; `main.py` gating (`[ASK]`, `--i-understand-this-is-live`)
   retained).
7. Follower-side lean of the leader data (HAR is one leader; our fills
   would move thinner books) — OBSERVED spread data is the proxy.

## Next milestones (require your input)
- **M4 testnet soak (≥2 weeks, fault injection):** needs testnet keys — I
  will ask before any key is configured.
- **M5 gated live:** requires all 6 gates green + your explicit sign-off.
  Nothing in this repo enables live by default, in tests, or in CI.
