# Collector README — Delta India daily option chain (read-only)

`collector.py` snapshots the **BTC / ETH / XAUT** daily option chain, the
underlying index and the l2 depth at the bid into `collector.db` (SQLite WAL).
No API key, no orders — market data only.

## Schedule (UTC)
| time | kind | purpose |
|---|---|---|
| 12:05 | entry | strategy sell-at-bid snapshot (all strikes ±10%, nearest 2 dailies) |
| 11:58 | preexpiry | last mark before expiry, also used for settlement index |
| 16:00 / 20:00 / 00:00 / 04:00 / 08:00 | watch | intraday mark & IV drift |

`settle_expired` runs after 12:05 / 11:58: for every expired `entry` snapshot it
computes settlement payoff from the underlying index at expiry (see API_NOTES.md —
there is **no settlement-price endpoint**, `settle_index_price` is approximated by
the underlying price of the last snapshot at/before expiry). Rows land in
`settlements` with per-contract P&L after the 1bp-notional fee (cap 3.5% of
premium) + 18% GST.

## Run on a laptop / cheap VPS
Option A — loop (simplest):
```bash
cd ~/fund_manager
nohup .venv/bin/python validate/collector.py --loop >> validate/collector.log 2>&1 &
```
Option B — cron `crontab -e`:
```
58 11 * * * cd ~/fund_manager && .venv/bin/python validate/collector.py --once preexpiry >> validate/collector.log 2>&1
05 12 * * * cd ~/fund_manager && .venv/bin/python validate/collector.py --once entry       >> validate/collector.log 2>&1
05 */4 * * * cd ~/fund_manager && .venv/bin/python validate/collector.py --once watch      >> validate/collector.log 2>&1
```
(`--once watch` will exit fast if nothing is due; entries/expiries are keyed so
duplicates are idempotent.)

Option C — systemd timer (`/etc/systemd/system/collector.service`):
```ini
[Unit]
Description=Delta India option chain collector
[Service]
WorkingDirectory=/home/USER/fund_manager
Type=oneshot
ExecStart=/home/USER/fund_manager/.venv/bin/python validate/collector.py --once adhoc
```
Timer every 15 min (`OnCalendar=*:0/15`) — `due_now()` in collector.py decides
which kind fires and nothing is recorded when no snapshot is scheduled.

## Robustness
* WAL mode, `INSERT OR REPLACE` idempotency, one row per (run, option) with `raw_json`.
* retries ×4 with backoff on 429/5xx/timeouts; failures logged in `runs`
  (`status='error'`) and `alerts`, the loop can't crash on one bad response.
* `watchdog()` raises an ALERT when the last `entry` snapshot is stale.
* All timestamps UTC (`+00:00`).

## Backup
```bash
sqlite3 validate/collector.db ".backup validate/backup_$(date +%F).db"
# or plain copy of collector.db + collector.db-wal after stopping the process
```
Recommended: nightly cron with the above line, and offsite copy of the .backup
(Dropbox/rclone/scp). DB growth: ~40KB/json-rich row ≈ 300 quotes × 10MB/day worst
case; set `weeks` pruning when you like — data volume is small at ~300–900 rows/day.

## Daily use
```bash
.venv/bin/python validate/analyze_options.py --size 1     # model-free analysis + sample_report.txt
.venv/bin/python validate/aio_bt.py                       # backtest, option-mode=mixed (uses real bids when present)
```
`validate/aio_bt.py` prints the REAL vs MODELLED share each run — e.g. after one
day: `REAL collected bids 41/6981 (1%)`. As the collector accrues weeks the real
share grows; do not judge the real-data option leg until ≥8 weeks are in.

## Schema (verified by schema.sql dump)
```
snapshots_2(snapshot_id PK, run_ts, kind, symbol, underlying, call_put, strike,
  expiry_ts, settlement_time, best_bid, best_ask, bid_size, ask_size,
  mark_price, mark_iv, bid_iv, ask_iv, delta, gamma, theta, vega,
  open_interest, volume, underlying_price, l2_bid_depth, contract_value, raw)
settlements(expiry_date, symbol PK+run_ts, underlying, call_put, strike,
  settle_index_price, entry_bid, payoff, pnl_per_contract, units_sold, run_ts, cv)
runs(run_ts, kind, status, detail, n_rows, duration_ms)
alerts(ts, message)
```
(legacy `snapshots` table retained but unused; empty.)
