# RUNBOOK — operations & incident procedures

## Start / stop (PAPER — the default and only unattended mode)

### All-day soak on a dedicated machine (gate-3 evidence)
```bash
scripts/paper_supervisor.sh start      # restart-on-crash loop, soak journal
scripts/paper_supervisor.sh status     # shows pid + gate-3 progress
scripts/paper_supervisor.sh stop       # graceful stop (writes state/soak_log.json)
```
- Evidence accumulates into `state/soak_log.json`; `scripts/paper_gate.py`
  converts it into `state/paper_gate.json` (gate 3 input) — re-run it
  periodically (or nightly via cron) once ≥28 days of soak exist.
- Optional autostart on login (survives reboots):
  ```bash
  cp deploy/systemd/fm-paper.service ~/.config/systemd/user/
  systemctl --user daemon-reload && systemctl --user enable --now fm-paper
  ```
- **The laptop must not sleep** to keep the soak continuous and stop WS/REST
  flaps: power settings → suspend OFF (or run `systemctl --user start` inside
  a session with `systemd-inhibit --what=sleep idle sleep infinity`).
- The collector (`validate/collector.py --loop`) should ALSO run
  continuously for gate 1 (≥8 weeks of option-chain snapshots) — same-laptop
  `tmux`/`systemd --user` is fine.
- Stop for real: `scripts/paper_supervisor.sh stop` then `rm KILL` when ready
  to resume.

### Single run (dev/testing)
```bash
python -m main --strategy credit_spread --mode paper --config config.yaml
# background with structured logs
nohup python -m main --strategy credit_spread --mode paper > logs/paper.out 2>&1 &
```
State lives in `state/` (SQLite + paper JSON). Kill switch: create file
`KILL` in the working dir or SIGTERM; both cancel open orders and stop
trading (existing `main.py` behavior).

**There is no documented way to run LIVE until M5.** When M5 arrives it will
require: all 6 gates green (`python scripts/golive_check.py` → exit 0),
testnet keys in the environment, and the CLI flag `--i-understand-this-is-live`
in a `--env prod` process. Ask the operator (you) before anything touches
real keys or money.

## Incident procedures

### Disconnect (WS drop / REST timeout)
1. The runner stops sending; fills arriving from the exchange via REST
   reconcile on the next poll.
2. On reconnect: `OrderBook.reconcile(open_orders_from_exchange)`.
   Exchange wins (status, filled_size). `trust_exchange:*` events are logged.
3. If the exchange forgets an order we believe is open → mismatch contains
   `halt:local_only_open:*` → flatten + halt. Investigate logs before restart.

### Partial fill
The OMS keeps `PARTIAL` with monotonic `filled_size`; average fill price is
volume-weighted. Stop-outs recompute against remaining size. Nothing special
to do manually unless repeated partials indicate a thin book — loosen budget
via `config/credit_spread.yaml` (smaller `notional_budget_usd`).

### Margin call / low available balance
`pre_trade` vetoes with `no_available_balance` / `margin_over_cap`. Reduce
`max_group_notional_usd` or let the day's loss limit halt the session. Never
add funds to defend a spread; the stop exists to make this unnecessary.

### Exchange outage
Kill switch file → cancel everything → stop. On restart, reconcile first
(chaos-restart semantics, `tests/test_chaos_restart.py`).

### Expiry day (options, 12:00 UTC expiry)
- Options on Delta rotate daily; settlement is approximated by the
  underlying index print at expiry (no settle-price endpoint — see
  `docs/API_NOTES.md`).
- Roll policy: `roll_before_expiry_min` minutes before expiry, re-evaluate
  with the far-dated chain; missed roll is safe by construction because
  every structure is defined-risk and carries its own stop.
- After settlement, mark spreads closed in the ledger before new entries
  (`max_open_spreads_per_underlying` counts open structures only).

### Reconciliation mismatch / unknown fill
`OrderBook halts` entries (`unknown_fill:*`, `overfill:*`) mean the local
state and the exchange disagree beyond repair → flatten, halt,
`reset_halt` is operator-only. Export `events` and compare against the
exchange admin report before resuming.

## Alerts (wired in M4+; check manually meanwhile)
Nightly: `python scripts/collector_health.py` (exit 0 = OK).
Weekly: `python scripts/walkforward_eval.py` (exit 2 = degraded;
`state/walkforward_log.json` accumulates every configuration tried).
Go-live posture: `python scripts/golive_check.py` (all FAIL is the correct
state pre-gates).

## Rollback
Container deploys tag the image with the git SHA; `docker run` the previous
SHA to roll back, plus a KILL file to stop trading immediately. Keep the last
two images on the host.

## Data hygiene
- `.gitignore` excludes HAR captures, leader CSVs, `state/`, caches.
- Never commit API keys; `gitleaks` runs in CI and pre-commit.
