# REPORT — Mirrorpip Leader 6001 "All-in-One Delta Trades" validation

Canonical report. Supersedes all earlier numbers in REPORT_FINAL.md and every
"live reference" figure used before the real exports arrived. Every number is
labelled **OBSERVED** (from the leader's real closed-trade export + exchange data)
or **MODELLED** (from this repo's engines/assumptions). Unlisted fees/funding are
explicitly **NOT OBSERVED** — see §9.

Stage-1 data as of 2026-10-22:

| Artifact | Status |
|---|---|
| `leader_6001_closed_trades_1792707421835.csv` (2,672 rows) | ingested into `leader_trades.db` via `validate/import_csv.py` (2,660 unique; 12 export rows were exact duplicates double-counting −$25.76) |
| HAR `trade.mirrorpip.com_Archive [26-10-23 03-46-23].har` (50 MB) | ingested via `validate/har_ingest.py`; contains platform config, 8-strategy leaderboard, positions snapshots — **no closed-trade history and no equity series** (2985 junk rows purged) |

## 1. Reconciliation (all OBSERVED)

Per month, closed-trade realized PnL by **exit month** (CSV = DB source `csv`),
vs the dashboard heatmap, vs Live PnL:

| Month | Realized (export) | Heatmap | Δ |
|---|---|---|---|
| 2024-09 | +9.45 | (pre-window) | — |
| 2024-10 | +395.57 | (pre-window) | — |
| 2025-01 | +5.00 | (pre-window) | — |
| 2025-08 | −4.37 | (pre-window) | — |
| 2025-09 | −88.11 | (pre-window) | — |
| 2025-10 | −29.55 | −29.55 | 0.00 |
| 2025-11 | −3.68 | −3.68 | 0.00 |
| 2025-12 | −129.59 | −130 | +0.41 |
| 2026-01 | −1.00 | −1.00 | 0.00 |
| 2026-02 | −11.93 | −11.93 | 0.00 |
| 2026-03 | +11.22 | +11.22 | 0.00 |
| 2026-04 | +210.30 | +210 | +0.30 |
| 2026-05 | +173.99 | +171 | +2.99 |
| 2026-06 | −346.93 | −347 | +0.07 |
| 2026-07 | +133.04 | +133 | +0.04 |
| 2026-08 | +415.90 | +415 | +0.90 |
| 2026-09 | +759.10 | +749 | +10.10 |
| 2026-10 (→22) | +38.35 | +39.13 | −0.78 |
| **Sum Oct-25→Oct-26** | **+1,219.31** | **+1,205 (≈Live PnL)** | +14.6 |

Reconciliations of the account-level views — **this resolves the earlier mystery**:

- **Cumulative realized all-history = +$1,536.77** (2,660 unique closed trades).
  The equity-chart endpoint (~$1,550) ≈ this: **the chart plots all-history
  realized PnL**, including the Sep/Oct-2024 (+$405) and pre-Oct-2025 blocks
  (+$317.52 total before Oct-2025).
- **Live PnL +$1,204.72 = realized −$332 open-book unrealized**: 1,536.77 −
  1,204.72 = −$332.05 implied current mark-to-market loss on ~23 open trades.
- The ~$14.6 heatmap-vs-realized gap (May/Sep/Oct) is accrual-vs-close timing;
  immaterial (≈1% of the record).
- **Fees and funding are NOT itemised in the export** (fees column = 0). Delta
  taker fees + 18% GST on 2,660 trades would reduce realized PnL by an unknown
  amount, likely material for lot-style turnover.

The account was **near-dormant Nov 2024–Aug 2025** (3 trades in ~10 months, max
1–2 concurrent lots): the flat/decaying chart of that era reflects that, not a
different strategy. Epoch prints inside `reconstructed_rules.json` "assets"
keys in an earlier run were a loop-variable shadowing bug — **fixed**; grouping
now correct.

## 2. Recovered rules (Stage 2, OBSERVED from fills)

Structure (per asset; source `validate/reconstructed_rules.json`):

| Rule | BTC | ETH | XAUT (gold) | PAXG (gold perp) |
|---|---|---|---|---|
| Options | Yes (C-/P- daily expiries) | Yes | Yes (XAUT) | No (perps only) |
| Short calls / short puts of options traded | 188/184 of 372□ | 225/117 of 342 | 205/281 of 486 | — |
| Bought options | small share (556 short vs 62 bought calls overall; 477 vs 105 puts) | same | same | — |
| Strike distance vs spot proxy, median | ≈ATM (+0.2%) | ≈ATM (−0.2%) | ≈ATM (−0.05%) | — |
| Short puts skew (median) | −0.03%–0% | −3.8% | −0.48% | — |
| Perp inventory | both directions, grid adds when price moves against book | | | strong Buy bias, long holds (median ~12 days) |
| Typical lot size | 0.001–0.05 BTC | 0.04–1 ETH | 0.015–0.25 | 0.05–0.4 |
| Median holding | ~9 days | ~10 days | ~10 days | ~12–27 days |
| Max concurrent open lots | ~60–109 | | | |
| SL/TP | **0 in every fill — no stop/target is exported** | | | |

□ counts are parse-coverage of ~372/342/486 option symbols (some symbols not
matched by the C/P-strike-expiry grammar). "Spot proxy" = nearest same-asset
perp fill within 24h (Delta perp ≈ spot).

Recovered risk controls (deriving from what is ABSENT is part of the finding):
- **No stop-loss, no take-profit** on any fill._exit is discretionary/time-based,
  weekly expiry rolls for options.
- Near-ATM strikes, daily expiries → strategy is short-vol premium harvesting.
- Net short options: 618 calls (556 sold) and 582 puts (477 sold) over the
  record; short-call PnL +$690.6, short-put +$244.4, bought options −$146.7.
- No evidence of a max-margin constraint: concurrent open trades reach 109 lots
  (Feb-2026) vs the $1,000 recommended margin — ledger-scale intensity of
  ~0.5%/day average return on that margin requires many multiples of the
  recommended margin or options-notional leverage far above $1,000 notional-equivalent.

## 3. Profit / loss anatomy

Instruments split (OBSERVED realized): options +$788.3 (66.4% trade win-rate),
perps +$748.5 (52.5%). Without the option short legs there is no profit.

Worst trades (all OBSERVED):
`C-BTC-63000-251024 Short −147.75`, `C-BTC-70000-290526 Buy −147.37`,
`BTCUSD Short −126.77/−126.47 (2026-08-20)`, `C-BTC-64000-310726 Short −117.10`.
Best: `ETHUSD Buy +252.68/+245.63 (2026-08-20)`, `BTCUSD Buy +194.28 (2024-10-14)`.

2026-08-20 is one intraday reversal day: big ETH long wins (~+$463 in 2 fills)
offset by BTC shorts losses (~$282) — event-driven directional scalp, binary by
nature.

## 4. The seven interrogation questions (Stage 3, OBSERVED unless noted)
(interrogate_results.json, fixed Q4/Q5 pipeline)

1. **Concentration.** Top-5 days = +$783.62 = **51.0% of all-history realized**.
   Top-20 trades = +$1,829.51 = **119% of trade PnL** (everything else nets
   −$292.7). Best month Sep-2026 +$759.10 = **62% of the 12-month record**;
   excluding it, 12 months = +$460.21. Oct-25→Jun-26 alone = **−$131.5**.
2. **Realized vs MTM.** Realized +$1,536.77; current open book ≈ −$332
   unrealised (OBSERVED by reconciliation, §1); fees + funding NOT OBSERVED,
   cuts net result by unknown but likely material amount.
3. **Jun-26 giveback.** Cumulative realized trough −$378.30 by 2026-06-20 =
   −37.8% of the $1,000 margin and −22.4× the average daily P&L move. Sold
   calls dominate: `C-BTC-64000-310726` −117.10 (6/15), `C-ETH-1700-120626`
   −96.93 and `C-BTC-63000-120626` −94.66 (6/12), `C-BTC-70000-050626` −78.73,
   plus ETHUSD/perp grid adds −116.48 (6/1), −22.93 (6/3). June month: calls
   −$236.2 vs puts +$34.6 vs perps −$145.3. Recovery took ~2.5 months (Jul +133,
   Aug +416).
4. **Distribution.** Monthly skew **+1.13**, excess kurtosis **1.65**, worst day
   **−$282.24 (−28% of margin)**, worst week **−$346.93 (−35%)**, longest
   underwater **98 days**, Ulcer index ≈ 42 (on cumulative-realized equity,
   scaled). Textbook short-vol signature.
5. **Significance.** 13 monthly observations, mean +$93.8, sd $268.9 →
   t = 1.259, p ≈ 0.23 — **not significant**. Daily calendar Sharpe 0.082
   (1.30 annualized) over 759 days is mechanically positive (deflated Sharpe
   haircut 0.15 → net +1.15 passes) but that result (a) rests on a
   non-stationary mix of a dormant year and a hot 3-month run and (b) ignores
   the 400-leader screening multiplicity the monthly test and §8 regime
   evidence already price in. Judged on months, luck is not excluded.
   TRADE-DAYS ONLY series (284 days, 37.3% of all calendar days in record;
   72% inside the 12-month active window) mean 0.54%/day = +$5.41/day on $1k.
6. **Follower reality.** HAR contains only a single 2,440-follower point and no
   follower-history series → join-time distribution CANNOT CONCLUDE from data.
   Arithmetic illustration (OBSERVED input only): a follower entering in
   Jul-2026 at $1,000 and staying through Oct-22 would have rode +133 (Jul),
   +416 (Aug), +759 (Sep), +38 (Oct) — but a follower entering in Apr-2026
   absorbed the Jun-26 −35% giveback of their entire margin.
7. **Survivorship.** The whole `get_leaders_list` is **8 strategies**. With
   2,440 followers on a 2-page leaderboard this leader is currently top-2-tier
   — but ranking 12 months ago (Jul–Sep 2025: −88 realized month, ~dormant
   before) is not derivable from any export in hand → rank-then-luck attribution
   CANNOT CONCLUDE.

## 5. Long-horizon backtest (Stage 4, MODELLED; sources: engine `aio_bt.py`,
   pipeline `longterm_bt.py`, results `results_longterm.json`)

Rules basis: **generic short-vol + gold/perp grid reconstruction** (the engine
predates Stage-2 recovery; it does not use the observed near-ATM-daily-XAUT
structure exactly). Label: illustrative reconstruction, not the leader.

| Config | PnL | Ann % | Sharpe | MaxDD | Days | Note |
|---|---|---|---|---|---|---|
| Grid only (no options) | −$10,005 (RUIN) | — | −4.28 | −100% | 213 day 2018 | ETH long grid dies in 2018 crash |
| No-safety replica | −$10,009 (RUIN) | — | −3.45 | −100% | 157 | |
| +caps only, options OFF | −$453 | −0.5% | 0.30 | 6.6% | 3353 | grid pillar worth nothing |
| +caps only (options ON) | +$14,076 | +10.0% | 0.81 | 13.2% | 3353 | **all profit from modelled option premium** |
| FINAL (+all layers) | −$1,233 | — | — | 12.4% | surv. by freezing | |

Stress (MODELLED, +caps config, % of $10k capital):

| Event | Day | Loss | × daily premium | % capital |
|---|---|---|---|---|
| COVID crash | 2020-03-12 | −$5,287 | −230× | −52.9% |
| May-2021 | 2021-05-19 | −$1,332 | −58× | −13.3% |
| Jun-2022 | 2022-06-13 | −$858 | −37× | −8.6% |
| FTX | 2022-11-08 | −$970 | −42× | −9.7% |
| Yen carry | 2024-08-05 | −$712 | −31× | −7.1% |
| Any ±10% BTC day | 14 days | −$5,635 med | — | −56% |

Sizing (bootstrap, capped config, MODELLED): floor 0.5×–4× leverage: P(ruin)<1%
OK; **5× = 23% ruin**.

Naive benchmark (MODELLED): short 1-day ATM BTC straddle at bid = **+$157k over
9y per $10k**, worst day −$2,157 (−21.6%) — the collector (real VR-premium
sampling) reproduces this shape. The leader's live record has the same
signature and a hotter P&L at much greater risk.

## 6. Calibration check vs the leader's real 12 months (new; done in
`calibrate_window.py` → `calibration_window.json`)

Run the capped / FINAL engines over exactly Oct-2025→Oct-2026 and rescale to
$1,000 margin. MODELLED monthly results vs OBSERVED:

| Month | Leader (OBS) | +caps (MOD) | FINAL (MOD) |
|---|---|---|---|
| 2025-10 | −29.55 | 0.00 | 0.00 |
| 2025-11 | −3.68 | +47.4 | +3.2 |
| 2025-12 | −129.60 | +50.6 | +18.6 |
| 2026-01 | −1.00 | +37.0 | +21.6 |
| 2026-02 | −11.93 | +35.3 | +3.7 |
| 2026-03 | +11.22 | +40.4 | +33.6 |
| 2026-04 | +210.30 | +33.8 | +27.2 |
| 2026-05 | +173.99 | +27.1 | +11.7 |
| 2026-06 | −346.93 | +25.9 | +6.5 |
| 2026-07 | +133.04 | +26.5 | +25.8 |
| 2026-08 | +415.90 | +28.7 | +31.3 |
| 2026-09 | +759.10 | +31.8 | +26.1 |
| 2026-10 | +38.35 | +20.0 | +1.7 |
| **Σ** | **+1,219.3** | **+405.0** | **+211.4** |

**Verdict from the fit:** the model produces a smooth +$20–50/month. It can
neither reproduce a −$130/−$347 month (it **understates risk**, no Jun-26-style
giveback exists at this sizing) nor a +$759 month (it is **missing intensity,
not structure** — the leader's realized variance per $1,000 margin is ≥10× the
model's and its profit arrived in 3 hot months). The leader's record is *more*
concentrated and *more* risky than a fair-sized replica of the same shape.

## 7. Verdicts on A / B / C

- **A. Is the reported record profitable? → SUPPORTED (as-reported cash),
  with 3 qualifications** — The exports reconcile: realized +$1,536.77 all-time;
  12 months Oct-25→Oct-26 = +$1,219.31; dashboard Live PnL +$1,204.72 = realized
  − $332 open-book MTM; chart endpoint ≈ realized. Currently the account is
  still underwater on ~23 open trades. Fees/funding are NOT OBSERVED and
  shrink the realized number. The "8% ROI" stat is a 1-month window only.
- **B. Distinguishable from luck and short-vol tail risk? → NOT SUPPORTED**
  Monthly t-stat 1.26 (p≈0.23) after 2,440 followers and a 400-leader-class
  screening space; 51% of all-time profit inside 5 days, 62% of the 12-month
  profit in one month, Jun-26 = −35% of margin, +1.13 monthly skew, hot
  Jul–Sep-2026 run right after a −35% giveback — all exactly the profile of
  short-vol carry with concentrated luck, and the modelled replica (§6)
  confirms the same structure's long-run ruin without caps. The record does
  NOT demonstrate edge; it demonstrates at least a real premium-capture
  capability whose survival depends on regime.
- **C. Replicable, and at what size and drawdown? → SUPPORTED for rule
  recovery; MODELLED risk: not at any size ≥ 5× without ruin.**
  Recovered: near-ATM daily short calls/puts BTC/ETH/XAUT + swing long-gold
  PAXG perps + grid BTC/ETH perp inventory, no stops. At the leader's own
  realized intensity, sizing >4× recommended margin exceeds 1% ruin
  (bootstrap + stress ×230 daily premium on COVID). At ≤1× with caps, expected
  return is ~+0.2%/day only while spot vol stays low — the same condition that
  precedes every listed stress event.

## 8. What is MODELLED / NOT modelled

- MODELLED: 2017+ proxy data (Binance 1h, PAXG-GC=F spliced gold, DVOL from
  2024-01, IV before that = realised ×2.0 call / ×0.9 put, 15% premium haircut,
  real Binance yearly funding averages), all long-horizon results, stress
  multiples, bootstrap sizing, naive straddle benchmark, and the
  §6 calibration engine run.
- NOT MODELLED (leaders rules, engine): leader's specific multi-leg lot mixing,
  exact roll clock (expiry-hour timing), discretionary closes, any seasonal or
  asset rotation the leader does but isn't captured in fills, GST/taker fees on
  the leader's own fills (NOT OBSERVED), intra-day open P&L of the ~23
  currently open trades, follower copy slippage (assumed, not measured), and
  the leader's true margin utilization vs the $1,000 recommended margin.
- NOT OBSERVABLE from the exports: 12-month-old leaderboard rank; follower
  join cohort; whether the leader selected assets knowing Q3 risk.

## 9. Data provenance & privacy

- `leader_6001_closed_trades_*.csv` (user's own export) → `import_csv.py` →
  `leader_trades.db` (source='csv', 2,660 rows). HAR → `har_ingest.py`
  (tokens/auth headers stripped; auth-header routes dropped). No credentials
  stored. No network access used in Stage-2/3; live collector
  (`collector.py`) still sampling Delta option bids separately.
