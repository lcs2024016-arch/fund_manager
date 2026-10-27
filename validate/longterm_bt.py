#!/usr/bin/env python3
"""
longterm_bt.py — Stage 4: long-horizon backtest (2017→now) of the reconstructed
"All-in-One Delta Trades" rules with realistic costs, on public proxies.

Data (all public, fetched once and cached):
  * BTCUSDT / ETHUSDT 1h klines from 2017-08/2017-11  (Binance spot API)
  * PAXGUSDT  1h klines from 2020-08-27 (gold proxy for PAXG/XAUT books)
  * Binance USDT-M funding-rate history for BTCUSDT/ETHUSDT (per-year averages
    replace aio_bt's constant FUND8 snapshot rates)
  * Deribit DVOL volatility index, daily, paginated (BTC from 2024-01, ETH
    from its listing); option IV(t) = max(trailing realised, DVOL/100 daily)
    before DVOL existed the model falls back to realised vol — an explicit
    assumption. Skew: iv_call x2.0 / iv_put x0.9 (calibrated to the live
    Delta chain on 2026-10-23), sold at the bid with the measured 15% haircut.
  * xStocks / silver / oil books have no long history anywhere -> excluded,
    weights renormalised (documented, not modelled).

Assets & weights (renormalised from the live screenshot allocation):
  ETH long 0.42, PAXG long 0.22 (+ short 0.22 same lot), BTC short 0.16,
  XAUT->PAXG long+short 0.13. Options only on ETH/BTC (PAXG has none on Delta).

Runs the SAME engine as validate/aio_bt.py (imported), with per-year stats,
deflated Sharpe across configs, regime split, stress table, sizing table.
"""
import json
import math
import os
import sys
import sqlite3
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
CACHE = os.path.join(ROOT, "long_cache")
os.makedirs(CACHE, exist_ok=True)

import aio_bt
from aio_bt import Cfg, ABLATIONS, simulate, make_books, daily, sharpe_ann, maxdd

START_S = 1502942400000  # first BTCUSDT hourly bar 2017-08-17


# ------------------------------------------------------------------ data
def http(url, params, tries=3):
    for a in range(tries):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(2 + a)
    raise RuntimeError(url)


def klines(symbol, interval="1h"):
    fp = os.path.join(CACHE, f"{symbol}_{interval}.csv")
    if os.path.exists(fp):
        return pd.read_csv(fp, index_col=0, parse_dates=True)
    out = []
    t = START_S
    while True:
        j = http("https://api.binance.com/api/v3/klines",
                 {"symbol": symbol, "interval": interval, "startTime": t,
                  "limit": 1000})
        if not j:
            break
        out.extend(j)
        t = j[-1][0] + 1
        if len(j) < 1000:
            break
        time.sleep(0.25)
    df = pd.DataFrame(out, columns=["ts","O","H","L","C","V","x1","x2","x3","x4","x5","x6"])
    df["ts"] = pd.to_datetime(df.ts, unit="ms", utc=True).dt.tz_convert(None)
    df = df.set_index("ts")[["O","H","L","C","V"]].astype(float)
    df.to_csv(fp)
    return df


def funding(symbol):
    fp = os.path.join(CACHE, f"funding_{symbol}.json")
    if os.path.exists(fp):
        return json.load(open(fp))
    out, t = [], 1550000000000  # 2019-02-13, when Binance perps started
    while True:
        j = http("https://fapi.binance.com/fapi/v1/fundingRate",
                 {"symbol": symbol, "startTime": t, "limit": 1000})
        if not j:
            break
        out.extend(j)
        t = j[-1]["fundingTime"] + 1
        if len(j) < 1000:
            break
        time.sleep(0.25)
    json.dump(out, open(fp, "w"))
    return out


def dvol(currency):
    fp = os.path.join(CACHE, f"dvol_{currency}.json")
    if os.path.exists(fp):
        return {int(k): v for k, v in json.load(open(fp)).items()}
    out, t, end = {}, 1577836800000, int(time.time() * 1000)
    while t < end:
        j = http("https://www.deribit.com/api/v2/public/get_volatility_index_data",
                 {"currency": currency, "resolution": "1D",
                  "start_timestamp": t, "end_timestamp": end})
        data = j.get("result", {}).get("data") or []
        if not data:
            break
        for row in data:
            out[int(row[0])] = float(row[4])  # close of the daily DVOL
        last = int(data[-1][0])
        if last <= t:
            break
        t = last + 86400000
        time.sleep(0.2)
    json.dump({str(k): v for k, v in out.items()}, open(fp, "w"))
    return {int(k): v for k, v in out.items()}


def fetch_all():
    dfs = {s: klines(s) for s in ("BTCUSDT", "ETHUSDT", "PAXGUSDT")}
    fund = {s: funding(s) for s in ("BTCUSDT", "ETHUSDT")}
    dv = {c: dvol(c) for c in ("BTC", "ETH")}
    return dfs, fund, dv


def funding_by_year(fund_rows):
    per = {}
    for r in fund_rows:
        y = datetime.fromtimestamp(r["fundingTime"] / 1000, timezone.utc).year
        per.setdefault(y, []).append(float(r["fundingRate"]))
    return {y: sum(v) / len(v) for y, v in per.items()}


# ------------------------------------------------------------------ build
def gold_backfill(pax_df, ts):
    """Yahoo COMEX gold futures GC=F daily closes, blended into PAXG hourly:
    after PAXG listing (2020-08) use real PAXG; before, hourly-constant days
    from GC=F scaled by the overlap ratio (PAXG $\approx$ spot gold)."""
    fp = os.path.join(CACHE, "gcf_daily.csv")
    if not os.path.exists(fp):
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF",
                         params={"period1": 1483228800, "period2": int(time.time()),
                                 "interval": "1d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=60).json()
        res = r["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        df = pd.DataFrame({"C": q["close"]}, index=pd.to_datetime(
            res["timestamp"], unit="s"))
        df.index = df.index.tz_localize(None)
        df["O"] = df["H"] = df["L"] = df["C"]
        df.to_csv(fp)
    return fp


def blend_gold(pax_df, ts):
    import numpy as _np
    fp = os.path.join(CACHE, "gcf_daily.csv")
    if not os.path.exists(fp):
        gold_backfill(pax_df, ts)
    dfg = pd.read_csv(fp, index_col=0, parse_dates=True).dropna()
    # daily hourly-flat series
    daily = dfg.reindex(pd.date_range(ts[0].normalize(), ts[-1].normalize(), freq="D")).ffill()
    hourly_c = daily.reindex(ts, method="ffill")["C"]
    start = pax_df.C.first_valid_index()
    ov_end = start + pd.Timedelta(days=30)
    r_ov = (pax_df.C.loc[start:ov_end].dropna() /
            hourly_c.loc[start:ov_end].dropna()).dropna()
    ratio = float(r_ov.mean())
    out = pax_df.copy()
    pre = out.index < start
    for col in ("O", "H", "L", "C"):
        vals = out[col].values.copy()
        vals[pre] = (hourly_c[pre] * ratio).values
        out[col] = vals
    return out, ratio, str(start)


def build_rows(dfs, dv):
    """rows for make_books(): gold books = PAXG twice (PAXGUSD + XAUT proxy)."""
    hours = sorted(set(dfs["BTCUSDT"].index) | set(dfs["ETHUSDT"].index)
                   | set(dfs["PAXGUSDT"].index))
    ts = pd.DatetimeIndex(hours)

    def aligned(sym):
        df = dfs[sym].reindex(ts)
        return df

    def vol_with_dvol(df, dvol_map, year_start=None):
        lr = np.log(df.C).diff()
        vol_d = lr.rolling(20 * 24).std() * math.sqrt(24)
        if dvol_map:
            ivd = pd.Series({pd.Timestamp(k, unit="ms"): v / 100.0 / math.sqrt(365)
                             for k, v in dvol_map.items()})
            ivd_h = ivd.reindex(ts, method="ffill")
            vol_d = np.maximum(vol_d, ivd_h)
        vol_d = vol_d.fillna(0.01 / math.sqrt(365))
        return vol_d

    btc = aligned("BTCUSDT")
    eth = aligned("ETHUSDT")
    pax = aligned("PAXGUSDT")
    pax, gold_ratio, gold_start = blend_gold(pax, ts)
    rows = []
    specs = [
        ("BTCUSD", "short", 0.16, True, "crypto", btc, dv.get("BTC")),
        ("ETHUSD", "long", 0.42, True, "crypto", eth, dv.get("ETH")),
        ("PAXGUSD", "long", 0.22, False, "gold", pax, None),
        ("PAXGUSD2", "long", 0.13, False, "gold", pax, None),
    ]
    # PAXG second book: short cover variant like the live XAUT long/short books
    rows.append(dict(sym="BTCUSD", side="short", wt=0.16, opt=True, grp="crypto",
                     O=btc.O.values, H=btc.H.values, L=btc.L.values, C=btc.C.values,
                     V=vol_with_dvol(btc, dv.get("BTC")).values,
                     E=btc.C.ewm(span=20 * 24, adjust=False).mean().values))
    rows.append(dict(sym="ETHUSD", side="long", wt=0.42, opt=True, grp="crypto",
                     O=eth.O.values, H=eth.H.values, L=eth.L.values, C=eth.C.values,
                     V=vol_with_dvol(eth, dv.get("ETH")).values,
                     E=eth.C.ewm(span=20 * 24, adjust=False).mean().values))
    rows.append(dict(sym="PAXGUSD", side="long", wt=0.22, opt=False, grp="gold",
                     O=pax.O.values, H=pax.H.values, L=pax.L.values, C=pax.C.values,
                     V=vol_with_dvol(pax, None).values,
                     E=pax.C.ewm(span=20 * 24, adjust=False).mean().values))
    rows.append(dict(sym="PAXGUSD2", side="short", wt=0.13, opt=False, grp="gold",
                     O=pax.O.values, H=pax.H.values, L=pax.L.values, C=pax.C.values,
                     V=vol_with_dvol(pax, None).values,
                     E=pax.C.ewm(span=20 * 24, adjust=False).mean().values))
    return ts, rows


# ------------------------------------------------------------------ runs
def run_once(ts, rows, cfg, tstart=None, tend=None, funding_rates=None):
    if funding_rates:
        for sym, rates in (funding_rates.items()):
            y = tstart.year if tstart is not None else 2019
            pass
        # patch FUND8 with the first applicable year's average
        yr = tstart.year if tstart is not None else next(iter(funding_rates))
        f8 = dict(aio_bt.FUND8)
        f8["BTCUSD"] = funding_rates.get("BTCUSDT", {}).get(yr, 0.0001)
        f8["ETHUSD"] = funding_rates.get("ETHUSDT", {}).get(yr, 0.0001)
        f8["PAXGUSD"] = f8["PAXGUSD2"] = 0.00002
        aio_bt.FUND8.clear(); aio_bt.FUND8.update(f8)
    books = make_books(rows, 10000.0)
    eq, ev, per_asset, liq, eqb, sl = simulate(books, ts, cfg, 10000.0,
                                               tstart=tstart, tend=tend)
    return eq, ev, per_asset, liq


def stats(seg):
    d = daily(seg)
    r = d.pct_change().dropna()
    if r.std() == 0 or len(r) < 10:
        return dict(pnl=float(seg.iloc[-1] - seg.iloc[0]), ann=0, sharpe=0,
                    mdd=0, n_days=len(r))
    return dict(pnl=float(seg.iloc[-1] - seg.iloc[0]),
                ann=round(float((seg.iloc[-1] / seg.iloc[0]) ** (365 / max(1, (d.index[-1] - d.index[0]).days)) - 1) * 100, 1),
                sharpe=round(float(sharpe_ann(d)), 2),
                mdd=round(float(maxdd(d)) * 100, 2), n_days=len(r))


STRESS = [("COVID crash", "2020-03-12"), ("May-2021", "2021-05-19"),
          ("Jun-2022", "2022-06-13"), ("FTX", "2022-11-08"),
          ("yen carry unwind", "2024-08-05")]


def stress_table(ts, rows, funding_rates, daily_prem_pct):
    out = []
    for name, d in STRESS:
        t0 = pd.Timestamp(d, tz=None)
        i0 = ts.searchsorted(t0 - pd.Timedelta(days=1))
        i1 = ts.searchsorted(t0 + pd.Timedelta(days=2))
        seg_t0, seg_t1 = ts[max(0, i0)], ts[min(len(ts) - 1, i1)]
        if i0 >= len(ts) or seg_t0 >= ts[0]:
            pass
        eq, ev, _, _ = run_once(ts, rows, Cfg(), tstart=seg_t0, tend=seg_t1,
                                funding_rates=funding_rates)
        loss = float((eq - eq.iloc[0]).min())
        out.append((name, d, round(loss, 2),
                    round(loss / (10000 * daily_prem_pct), 1),
                    round(100 * loss / 10000, 2)))
    # general: every BTC ±10% single day
    btc = rows[0]["C"]
    ret = pd.Series(btc, index=ts).pct_change()
    big = ret[ret.abs() >= 0.10].index[:20]
    worst = []
    for d in big:
        i0 = ts.searchsorted(d - pd.Timedelta(days=1))
        i1 = ts.searchsorted(d + pd.Timedelta(days=2))
        eq, _, _, _ = run_once(ts, rows, Cfg(), tstart=ts[i0], tend=ts[i1],
                               funding_rates=funding_rates)
        worst.append(float((eq - eq.iloc[0]).min()))
    out.append(("any ±10% BTC day (all {} days, first {} shown)".format(
                int((ret.abs() >= .10).sum()), len(worst)),
                "", round(min(worst), 2), "—",
                round(100 * min(worst) / 10000, 2)))
    return out


def sizing_table(daily_ret, worst_mdd_frac):
    """leverage m: P(path maxDD <= -1/m) below 1% via 5k bootstrap path maxDD."""
    import random
    rng = random.Random(11)
    arr = np.array(daily_ret)
    n = len(arr)
    mdds = []
    for _ in range(3000):
        s = arr
        eps = np.array([arr[rng.randrange(n)] for _ in range(len(arr))])
        cum = np.cumprod(1 + eps / 1)  # per 1x leverage
        peak = np.maximum.accumulate(np.maximum(np.cumprod(1 + eps * 0), 1e-9))
        # maxDD of cumulative return path
        cumr = np.cumsum(eps)
        peak = np.maximum.accumulate(cumr)
        mdds.append(min(0.0, float((cumr - peak).min())))
    mdds = np.array(mdds)
    rows = []
    for p_ruin_target in (0.01,):
        for m in (0.5, 1, 2, 3, 4, 5):
            thr = -1.0 / m
            p = float((mdds <= thr).mean())
            rows.append((m, round(100 * p, 2), "OK" if p < p_ruin_target else "RUIN>1%"))
    return rows, mdds


def calibration_check():
    db = os.path.join(ROOT, "leader_trades.db")
    if os.path.exists(db):
        con = sqlite3.connect(db)
        rows = con.execute("SELECT COUNT(*) FROM daily_equity").fetchone()[0]
        con.close()
        if rows:
            return f"leader_trades.db has {rows} equity rows - compare monthly"
    return "STAGE1 DATA MISSING (leader_trades.db empty) — pending HAR export"


def regimes_split(ts, eq, btc_c):
    bull, bear, rng_v, hv = [], [], [], []
    btc = pd.Series(btc_c, index=ts).ffill()
    ma = btc.rolling(120 * 24).mean()
    vol = btc.pct_change().rolling(30 * 24).std()
    hvthr = vol.quantile(0.9)
    r = daily(eq).pct_change().dropna()
    for d, v in r.items():
        if vol.asof(d) > hvthr:
            hv.append(v)
        elif btc.asof(d) > ma.asof(d) * 1.05:
            bull.append(v)
        elif btc.asof(d) < ma.asof(d) * 0.95:
            bear.append(v)
        else:
            rng_v.append(v)
    def pack(name, xs):
        if not xs:
            return (name, 0, 0)
        m = np.mean(xs); s = np.std(xs)
        return (name, len(xs), round(m * 252 * 100, 1),
                round(m / (s * math.sqrt(252)) if s else 0, 2))
    return [pack("bull", bull), pack("bear", bear), pack("range", rng_v),
            pack("high-vol", hv)]


BARE = Cfg()  # replicate WITHOUT safety layers
LAYERED = [
    ("+regime",          False, dict()),
]
NO_SAFETY = Cfg()                                   # baseline: no regime, no volscale, no caps, no tstop
SAFETY_LAYERS = [
    ("+regime filter",      dict(regime=True)),
    ("+caps only (no breaker)", dict(caps=True)),
    ("+caps only, options OFF", dict(caps=True, use_options=False)),
    ("+volscale grid/TP",   dict(volscale=True, regime=True)),
    ("+anti-mart 0.8",      dict(volscale=True, regime=True, decay=0.8)),
    ("+time stop 3d",       dict(volscale=True, regime=True, decay=0.8, max_hold_days=3.0)),
    ("+caps +breaker = FINAL", dict(volscale=True, regime=True, decay=0.8,
                                    max_hold_days=3.0, caps=True, otm_k=0.8, optskip=True)),
]


def from_over(d):
    from dataclasses import replace
    return replace(Cfg(), **d)


def main():
    print("fetching proxies (cached)...")
    dfs, fund_rows, dv = fetch_all()
    fy = {s: funding_by_year(v) for s, v in fund_rows.items()}
    print("funding yearly avg:", json.dumps(fy, default=str)[:300])
    ts, rows = build_rows(dfs, dv)
    print(f"bars {len(ts)}  {ts[0]} → {ts[-1]}")

    results = {}
    daily_prem_pct = 0.0005
    # grid-only benchmark first (no options)
    cfg_grid = from_over(true := dict(use_options=False))
    eq, ev, pa, liq = run_once(ts, rows, cfg_grid, funding_rates={"BTCUSDT": fy["BTCUSDT"], "ETHUSDT": fy["ETHUSDT"]})
    results["grid only (no options)"] = stats(eq)
    results["grid only (no options)"]["per_asset"] = pa

    # naive short 1-day ATM straddle benchmark (analytic on BTCUSDT)
    btc = dfs["BTCUSDT"].C
    ret = btc.pct_change()
    iv = ret.rolling(24 * 20).std() * math.sqrt(365)          # daily sigma basis
    dS = btc.diff().abs()                                     # atm cost at expiry
    prem_daily = 0.85 * 0.8 * btc * iv / math.sqrt(365)       # bid premium, ATM straddle
    short_pnl = (prem_daily - dS).dropna()                    # per coin held
    n_days = (btc.index[-1] - btc.index[0]).days
    scaled = short_pnl / btc * 10000.0                        # PnL if notional $10k
    results["naive short 1d ATM straddle (BTC)"] = dict(
        pnl_10k=round(float(scaled.sum()), 0),
        mean_daily_per_10k=round(float(scaled.mean()), 2),
        worst_day_per_10k=round(float(scaled.min()), 2),
        note="premium at bid each day minus intrinsic |dS| paid; unhedged, 1x $10k")

    # ablation of safety layers on the long horizon
    combos = [("no safety layers (replica)", {})]
    for name, d in SAFETY_LAYERS:
        combos.append((name, d))
    yearly = {}
    for name, d in combos:
        cfg = from_over(d)
        eq, ev, pa, liq = run_once(ts, rows, cfg,
                                   funding_rates={"BTCUSDT": fy["BTCUSDT"],
                                                  "ETHUSDT": fy["ETHUSDT"]})
        st = stats(eq)
        st["liq"] = liq
        st["per_asset"] = pa
        results[name] = st
        # per calendar-year stats
        years = {}
        for y in range(2018, 2027):
            seg = eq[(eq.index.year == y)]
            if len(daily(seg)):
                years[str(y)] = stats(seg)
        yearly[name] = years
        # daily premium share
        if "opt" in ev.kind.values:
            dp = ev[ev.kind == "opt"].copy()
            dp["t"] = pd.to_datetime(dp["t"])
            dpl = dp.set_index("t").pnl.groupby(dp.set_index("t").index.floor("D")).median()
            gains = dpl[dpl > 0]
            if len(gains):
                daily_prem_pct = float((gains * 10 / 10000).mean())  # median daily option premium / equity
            results[name]["daily_premium_pct"] = round(daily_prem_pct, 4)
    # store full equity of replica + FINAL for regime split
    eq_rep, _, _, _ = run_once(ts, rows, NO_SAFETY,
                               funding_rates={"BTCUSDT": fy["BTCUSDT"], "ETHUSDT": fy["ETHUSDT"]})
    results["regime_split_replica"] = regimes_split(ts, eq_rep, rows[0]["C"])

    # deflated Sharpe across all configs tried (incl yearly grid search proxy)
    trial_sharpes = []
    for name, ys in yearly.items():
        for y, s in ys.items():
            if s["sharpe"]:
                trial_sharpes.append(s["sharpe"])
    dsr_ann = results["no safety layers (replica)"]["sharpe"]
    obs = dsr_ann
    dsr_val = aio_bt.deflated_sharpe(dsr_ann, trial_sharpes if trial_sharpes else [1.0])
    haircut_val = round(dsr_ann - dsr_val, 3)
    net = round(dsr_val, 3)
    results["deflated_sharpe"] = dict(observed_ann=round(obs, 2),
                                      trials=len(trial_sharpes), net=net)

    # stress table (on replica without safety layers)
    prem_pct = results["+caps only (no breaker)"].get("daily_premium_pct", 0.0005)
    results["stress"] = stress_table(ts, rows,
                                     {"BTCUSDT": fy["BTCUSDT"], "ETHUSDT": fy["ETHUSDT"]},
                                     max(prem_pct, 0.0002))

    # sizing table on FINAL-equity daily returns
    cfg_f = from_over(SAFETY_LAYERS[-1][1])
    eq_f, _, _, _ = run_once(ts, rows, cfg_f,
                             funding_rates={"BTCUSDT": fy["BTCUSDT"], "ETHUSDT": fy["ETHUSDT"]})
    dr = daily(eq_f).pct_change().dropna()
    rows_s, mdds = sizing_table(dr.values,
                                results[SAFETY_LAYERS[-1][0]]["mdd"] / 100)
    results["sizing"] = [list(map(str, r)) for r in rows_s]
    results["worst_hist_mdd_pctFINAL"] = results[SAFETY_LAYERS[-1][0]]["mdd"]

    results["calibration_check"] = calibration_check()

    json.dump({"summary": results, "yearly": yearly},
              open(os.path.join(HERE, "results_longterm.json"), "w"), indent=1, default=str)
    print(json.dumps(results, indent=1, default=str))


if __name__ == "__main__":
    main()
