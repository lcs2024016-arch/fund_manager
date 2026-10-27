# CHANGELOG

## 2026-10-23 (later) — all-day paper soak tooling + runner adapter
- `strategy/credit_spread.CreditSpreadRunner` — StrategyPort/I/O adapter
  feeding real option-chain tickers into the pure core; option universe
  discovery (305 BTC / 220 ETH live symbols), position-truth open-spread
  sync, pre-trade risk veto wired to `main.py --strategy credit_spread`.
  DDMMYY option symbol grammar pinned by tests; live 90-s dry-run clean.
- `scripts/paper_supervisor.sh` — restart-on-crash all-day loop with
  KILL-file stop; `scripts/paper_gate.py` accumulates gate-3 evidence into
  `state/paper_gate.json` (28-day soak + parity + zero unresolved mismatches).
- `deploy/systemd/fm-paper.service` — autostart-on-login unit.
- `main.py` poll loop scoped for credit_spread (underlying candles only;
  rate-limit friendly).

## 2026-10-23 — M1/M2/M3: risk-first core, paper execution processes, CI/CD

Added — strategy & risk (M1):
- `config/credit_spread.yaml`, `config/risk_limits.yaml` (typed, git-versioned).
- `src/strategy/credit_spread.py` — pure deterministic strategy core
  (delta-band short verticals, slippage guard, credit≥2×fee, stop at
  2× credit, per-underlying open cap, budget-from-max-loss sizing).
- `src/core/portfolio_risk.py` — RiskLimits + exposure (per-asset and
  correlated-group caps), MTM open-book limit, daily/weekly loss limits,
  portfolio drawdown breaker (flatten+halt, operator reset), pre-trade
  veto gate.
- `src/core/options.py` — payoff/settlement/spread math,
  defined-risk validation.

Added — execution & OMS (M2):
- `src/core/oms.py` — order state machine (new/open/partial/filled/
  cancelled/rejected/unknown), idempotent submits, overfill & unknown-fill
  halts, exchange-trusted reconciliation.
- `src/core/conversions.py` — per-coin premium units (1000x-bug guards).
- `src/core/fees.py` — VERIFIED option fee (1bp notional cap 3.5% premium,
  ×1.18 GST) + UNVERIFIED linear defaults flagged for pre-live verification.

Added — gates & ops:
- `scripts/golive_check.py` — 6 gates, prints which gate failed and why,
  JSON mode with GATES_JSON_BEGIN/END markers, exits 1 on any fail
  (verified failing-closed: collector needs ≥8 weeks of snapshots;
  3 state soaks pending real runtime).
- `scripts/collector_health.py`, `scripts/walkforward_eval.py` +
  `state/walkforward_log.json`.

Added — tests (36 new + integrations, 232 total passing):
unit / property (hypothesis) / fake-exchange (429s, rejects, duplicate and
out-of-order fills, disconnect-reconcile) / backtest-vs-paper parity via
httpx.MockTransport with a recorded real BTCUSD products fixture /
golden backtest regression (`tests/golden_backtest.json`, review-gated,
GOLDEN_UPDATE=1 documented) / chaos crash-restart / go-live gates /
June-2026 tail reproduction & breaker-halts-before-bleed tests.

Added — CI/CD (M3):
- `.github/workflows/ci.yml`: ruff, mypy --strict (9 risk-critical files),
  gitleaks, pip-audit, secret-pattern scan, pytest with 85% coverage gate,
  golden regression, docker build + trivy (tagged per SHA).
- `.github/workflows/scheduled.yml`: nightly collector health + backup
  artifact, weekly walk-forward issue on degradation, weekly dependency audit.
- `docker/Dockerfile` non-root pinned base, paper-only default CMD.
- `.pre-commit-config.yaml` mirrors lint/type/test/secret gates.

Docs: ARCHITECTURE.md, RUNBOOK.md, RISK.md, docs/API_NOTES.md, this file.

## Verdict carried over from validate/ (2026-10-22)
Research verdict: NO validated leader edge → bot stays in PAPER until all
six gates pass AND the operator passes the explicit live flag. Every figure
in reports is labelled OBSERVED or MODELLED.
