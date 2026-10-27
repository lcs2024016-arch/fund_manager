#!/usr/bin/env python3
"""
Realistic backtest + validation of a reconstruction of Mirrorpip's
"All-in-One Delta Trades" (Delta Exchange India).  v2 — see README.md.

Reality baked in (verified against api.india.delta.exchange, 2026-10-23):
 * Options listed ONLY for BTC / ETH / XAUT.  All other books are LIFO-only.
 * Expired option candles NOT retrievable -> BS(1 day) premium with
   IV = trailing realised vol * iv_mult (calibrated to live chain:
   iv_mult_call=2.0, iv_mult_put=0.9), ALWAYS sold at the bid (haircut).
 * Fees (published tier-0): perp taker 5bp/maker 2bp; options 1bp notional
   capped at 3.5% of premium; settlement 1.5bp capped 5% premium; +18% GST.
 * Funding: constant signed 8h snapshot rate on marked notional (no public
   history endpoint).  'FINAL funding off' ablation shows sensitivity.
 * Pessimistic fills: limits must trade THROUGH; ambiguous bars -> worse
   outcome; 5bp taker slippage; option sold at 12:00 UTC, settled at 12:00 UTC.
 * Cross margin: 15%/10% initial/maintenance on open notional; worst
   underwater book liquidated at close with 2% penalty.  Circuit breaker
   freezes entries at portfolio drawdown > dd_break.

Run:
  .venv/bin/python validate/aio_bt.py                  # ablation table (real data)
  .venv/bin/python validate/aio_bt.py --walkforward     # validation suite
  .venv/bin/python validate/aio_bt.py --synthetic       # random-walk control
"""
import argparse, json, math, os, time
from dataclasses import dataclass, field, replace
from math import erf, log, sqrt, sqrt as _sq

import numpy as np
import pandas as pd
import requests

BASE = "https://api.india.delta.exchange"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "delta_cache")
DAY = 24
WARMUP = 24 * 20

FEE_PERP_TAKER, FEE_PERP_MAKER = 0.0005, 0.0002
FEE_OPT_NOTIONAL, FEE_OPT_PREM_CAP = 0.0001, 0.035
FEE_SETTLE_NOTIONAL, FEE_SETTLE_PREM_CAP = 0.00015, 0.05
GST = 0.18
SLIP = 0.0005
SPREAD_HAIRCUT = 0.15
INITM, MAINTM, LIQ_PEN = 0.15, 0.10, 0.02
IV_CALL_MULT, IV_PUT_MULT = 2.0, 0.9
OPT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collector.db")
CONTRACT_VALUE = {"BTCUSD": None, "ETHUSD": None, "XAUTUSD": None}
# Delta quotes option prices per FULL coin (USD premium for 1 coin; per-contract $
# = quote * contract_value where cv = 0.001 BTC / 0.010 ETH / 0.001 XAUT on
# 2026-10-23 chain).  Therefore per-underlying-unit premium = the quoted price.
CV = 1.0


def load_real_options(db_path=OPT_DB):
    """Maps (date, underlying-UA, call/put)->(symbol, strike, bid, settlement_payoff)
    from the collector's 12:05 'entry' snapshots and settlements table."""
    import sqlite3
    out = {}
    if not os.path.exists(db_path):
        return out
    con = sqlite3.connect(db_path, timeout=30)
    try:
        settles = {}
        for sym, payoff, cv in con.execute(
                "SELECT symbol, payoff, COALESCE(cv, 0.001) FROM settlements"):
            settles[sym] = float(payoff) / (cv or 0.001)   # payoff per underlying unit
        rows = con.execute(
            "SELECT run_ts, underlying, call_put, symbol, strike, best_bid, expiry_ts "
            "FROM snapshots_2 WHERE kind IN ('entry') AND best_bid IS NOT NULL").fetchall()
        for run_ts, ua, side, sym, strike, bid, exp in rows:
            date = run_ts[:10]
            key = (date, ua, side)
            cur = out.get(key)
            # keep the quote whose expiry is the nearest daily (first row since one run per day)
            if cur is None or exp < cur["expiry"]:
                out[key] = dict(symbol=sym, strike=float(strike), bid=float(bid),
                                payoff=settles.get(sym), expiry=exp)
    finally:
        con.close()
    return out


REAL_OPTS = {}
REAL_STATS = {"real": 0, "model": 0, "real_payoff": 0, "model_payoff": 0}


FUND8 = {"BTCUSD": .000086, "ETHUSD": .000077, "PAXGUSD": .00005, "XAUTUSD": .00005,
         "SLVONUSD": .0001, "OILUSD": .0001, "QQQXUSD": .0001, "SPYXUSD": .0001,
         "AAPLXUSD": .0001, "AMZNXUSD": .0001, "TSLAXUSD": .0001, "NVDAXUSD": .0001,
         "BABABUSD": .0001}

ASSETS = [  # (symbol, sides, weight, options?, group)
    ("ETHUSD",  ("long",),          0.42, True,  "crypto"),
    ("PAXGUSD", ("long", "short"),  0.22, False, "gold"),
    ("BTCUSD",  ("short",),         0.16, True,  "crypto"),
    ("XAUTUSD", ("long", "short"),  0.13, True,  "gold"),
    ("QQQXUSD", ("long",),          0.05, False, "stocks"),
    ("BABABUSD",("long",),          0.01, False, "stocks"),
    ("SPYXUSD", ("long",),          0.03, False, "stocks"),
    ("AAPLXUSD",("long",),          0.02, False, "stocks"),
    ("TSLAXUSD",("long",),          0.02, False, "stocks"),
    ("NVDAXUSD",("long",),          0.02, False, "stocks"),
    ("SLVONUSD",("long", "short"),  0.01, False, "silver"),
    ("OILUSD",  ("long", "short"),  0.01, False, "oil"),
]


# ----------------------------------------------------------------- data
def _epoch(x):
    t = pd.Timestamp(x)
    if t.tz is None:
        t = t.tz_localize("UTC")
    return int(t.tz_convert("UTC").timestamp())


def fetch_candles(symbol, res="1h", start=None, end=None):
    s, e = _epoch(start), _epoch(end)
    os.makedirs(CACHE, exist_ok=True)
    fn = f"{CACHE}/{symbol}_{res}_{s//86400}_{e//86400}.csv"
    if os.path.exists(fn):
        df = pd.read_csv(fn, index_col=0); df.index = pd.to_datetime(df.index, utc=True)
        return df
    rows, t = [], s
    while t < e:
        t2 = min(t + 3600 * 1900, e)
        r = requests.get(f"{BASE}/v2/history/candles",
                         params=dict(symbol=symbol, resolution=res, start=t, end=t2), timeout=30)
        r.raise_for_status(); rows += r.json().get("result", []); t = t2
        time.sleep(0.15)
    if not rows:
        raise RuntimeError(f"no candles for {symbol}")
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = (df.drop_duplicates("time").sort_values("time").set_index("time")
            [["open", "high", "low", "close"]].astype(float))
    df.to_csv(fn); return df


def synthetic_candles(symbol, start, end, seed):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, end, freq="3600s", tz="UTC")
    vol = (0.45 if symbol in ("BTCUSD", "ETHUSD") else 0.16) / sqrt(DAY * 365)
    r = rng.normal(0, vol, len(idx))
    close = 1000 * np.exp(np.cumsum(r))
    op = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, vol * 0.5, len(idx)))
    return pd.DataFrame({"open": op, "high": np.maximum(op, close) * (1 + wick),
                         "low": np.minimum(op, close) * (1 - wick), "close": close}, index=idx)


def load_arrays(sym, start, end, synthetic=False):
    """Returns (df, vol_d, ema) — vol_d/f trailing daily sigma, ema = 20-day EMA; trailing only."""
    df = (synthetic_candles(sym, start, end, hash(sym) % 2**31) if synthetic
          else fetch_candles(sym, "1h", start, end))
    lr = np.log(df.close).diff()
    vol_d = lr.rolling(20 * DAY).std() * sqrt(DAY)
    ema = df.close.ewm(span=20 * DAY, adjust=False).mean()
    return df, vol_d, ema


# ----------------------------------------------------------------- pricing
def ncdf(x): return 0.5 * (1 + erf(x / sqrt(2)))


def bs(S, K, T, sig, call):
    if T <= 0 or sig <= 0:
        return max(S - K, 0) if call else max(K - S, 0)
    d1 = (log(S / K) + 0.5 * sig * sig * T) / (sig * sqrt(T))
    d2 = d1 - sig * sqrt(T)
    return S * ncdf(d1) - K * ncdf(d2) if call else K * ncdf(-d2) - S * ncdf(-d1)


# ----------------------------------------------------------------- config
@dataclass
class Cfg:
    k: float = 1.2            # grid = k * trailing daily sigma (if volscale)
    rat: float = 0.6          # TP = rat * grid
    otm_k: float = 0.6        # strike = otm_k * daily sigma OTM
    lot_frac: float = 0.10
    max_lots: int = 8
    decay: float = 1.0        # anti-martingale decay
    stop: float = 0.25        # book stop as fraction of book capital
    dd_break: float = 0.12
    max_hold_days: float = 0.0
    regime: bool = False
    volscale: bool = False
    optskip: bool = False
    caps: bool = False
    use_options: bool = True
    funding: bool = True
    opt_mode: str = "model"
    haircut: float = SPREAD_HAIRCUT
    iv_call: float = IV_CALL_MULT
    iv_put: float = IV_PUT_MULT
    group_caps: dict = field(default_factory=lambda: {"crypto": 0.50, "gold": 0.30,
                                                      "stocks": 0.25, "silver": 0.10, "oil": 0.10})


ABLATIONS = [
    ("baseline",              Cfg()),
    ("+regime(EMA20d)",       replace(Cfg(), regime=True)),
    ("+volscale grid/TP",     replace(Cfg(), volscale=True)),
    ("+anti-mart 0.8",        replace(Cfg(), decay=0.8)),
    ("+sigma strike 0.8",     replace(Cfg(), otm_k=0.8)),
    ("+skip thin/breakout",   replace(Cfg(), optskip=True)),
    ("+time stop 3d",         replace(Cfg(), max_hold_days=3.0)),
    ("+caps+breaker",         replace(Cfg(), caps=True)),
    ("FINAL (all)",           replace(Cfg(), regime=True, volscale=True, decay=0.8,
                                      otm_k=0.8, optskip=True, max_hold_days=3.0, caps=True)),
    ("FINAL minus options",   replace(Cfg(), regime=True, volscale=True, decay=0.8,
                                      otm_k=0.8, optskip=True, max_hold_days=3.0, caps=True,
                                      use_options=False)),
    ("FINAL funding off",     replace(Cfg(), regime=True, volscale=True, decay=0.8,
                                      otm_k=0.8, optskip=True, max_hold_days=3.0, caps=True,
                                      funding=False)),
    ("FINAL haircut 30%",     replace(Cfg(), regime=True, volscale=True, decay=0.8,
                                      otm_k=0.8, optskip=True, max_hold_days=3.0, caps=True,
                                      haircut=0.30)),
    ("FINAL IV call 1.2x",    replace(Cfg(), regime=True, volscale=True, decay=0.8,
                                      otm_k=0.8, optskip=True, max_hold_days=3.0, caps=True,
                                      iv_call=1.2)),
    ("FINAL no options+tstop 6d", replace(Cfg(), regime=True, volscale=True, decay=0.8,
                                      otm_k=0.8, optskip=True, max_hold_days=6.0, caps=True,
                                      use_options=False)),
]

FINAL = ABLATIONS[8][1]

def first_finite(arr):  # for B&H on sparse books
    a = np.asarray(arr, dtype=float)
    idx = np.flatnonzero(np.isfinite(a) & (np.arange(len(a)) >= WARMUP))
    return int(idx[0]) if len(idx) else None


# ----------------------------------------------------------------- engine
class Book:
    __slots__ = ("sym","side","sgn","wt","opt_able","group","caps_ok",
                 "O","H","L","C","vol","ema",
                 "capital","lots","realized","opt","cool","events","notional","by")


def make_books(rows, capital):
    tw = sum(r["wt"] for r in rows)
    books = []
    for r in rows:
        b = Book()
        b.sym, b.side, b.sgn = r["sym"], r["side"], (1 if r["side"] == "long" else -1)
        b.sgn = 1 if r["side"] == "long" else -1
        b.wt, b.opt_able, b.group = r["wt"], r["opt"], r["grp"]
        b.O, b.H, b.L, b.C, b.vol, b.ema = r["O"], r["H"], r["L"], r["C"], r["V"], r["E"]
        b.capital = capital * r["wt"] / tw
        b.lots, b.realized, b.opt, b.cool, b.events, b.notional = [], 0.0, None, 0, [], 0.0
        books.append(b)
    return books


def simulate(books, ts, cfg, capital=10000.0, tstart=None, tend=None):
    """ts: DatetimeIndex (hourly union). If tstart/tend given, slice arrays by position."""
    t0 = ts.searchsorted(pd.Timestamp(tstart)) if tstart is not None else 0
    t1 = ts.searchsorted(pd.Timestamp(tend)) if tend is not None else len(ts)
    n = t1 - t0
    nbooks = len(books)
    eq = np.zeros(n); eq[0] = capital
    eqb = np.zeros((nbooks, n))
    peak, breaker, liq = capital, False, 0

    for i in range(1, n):
        prev = eq[i - 1]
        if prev > peak: peak = prev
        breaker = cfg.caps and (peak - prev) / peak > cfg.dd_break
        t = ts[t0 + i].time()

        gnot = {}
        if cfg.caps:
            for b in books:
                if b.lots:
                    gnot[b.group] = gnot.get(b.group, 0.0)

        port_eq = 0.0
        for bi, b in enumerate(books):
            o, h, l, c = b.O[t0 + i], b.H[t0 + i], b.L[t0 + i], b.C[t0 + i]
            if not (np.isfinite(o) and np.isfinite(c)):
                eqb[bi, i] = eqb[bi, i - 1]; port_eq += eqb[bi, i]
                continue
            vol_d = b.vol[t0 + i]
            sig_d = float(vol_d) if np.isfinite(vol_d) and vol_d > 0 else 0.35 / sqrt(365)
            sig_a = sig_d * sqrt(365)
            grid_w = cfg.k * sig_d if cfg.volscale else 0.010
            tp_w = cfg.rat * grid_w
            ema_ok = True
            if cfg.regime and np.isfinite(b.ema[t0 + i]):
                ema_ok = (c > b.ema[t0 + i]) if b.sgn == 1 else (c < b.ema[t0 + i])

            # 1a) settle expiring daily option at bar open
            if b.opt is not None:
                K, units, call, prem_net = b.opt["K"], b.opt["units"], b.opt["call"], b.opt["prem_net"]
                real_pay = b.opt.get("real_payoff_unit")
                if real_pay is not None:
                    pay = real_pay
                    REAL_STATS["real_payoff"] += 1
                else:
                    pay = max(o - K, 0) if call else max(K - o, 0)
                    REAL_STATS["model_payoff"] += 1
                fee = (min(FEE_SETTLE_NOTIONAL * o * units, FEE_SETTLE_PREM_CAP * pay * units)
                       * (1 + GST)) if pay > 0 else 0.0
                pnl = prem_net - pay * units - fee
                b.realized += pnl
                b.events.append((i, ts[t0 + i], "opt", pnl))
                b.opt = None
            # 1b) sell today's 1-day option at 12:00 UTC (bid)
            if b.opt_able and cfg.use_options and t.hour == 12 and t.minute == 0 and b.lots:
                units = sum(q for _, q, _ in b.lots)
                call = b.sgn == 1
                real = REAL_OPTS.get((str(ts[t0 + i].date()), b.sym.replace("USD", ""),
                                      "call" if call else "put"))
                use_real = (cfg.opt_mode == "mixed" and real is not None and real["bid"] > 0)
                if use_real:
                    K = real["strike"]
                    prem_mid = prem_bid = real["bid"] * CV        # real bid price, no haircut
                    sig_p = sig_a                                 # only used for liability marks
                    REAL_STATS["real"] += 1
                else:
                    K = o * (1 + b.sgn * (0.003 if not cfg.volscale else cfg.otm_k * sig_d))
                    sig_p = sig_a * (cfg.iv_call if call else cfg.iv_put)
                    prem_mid = bs(o, K, 1 / 365, sig_p, call)
                    prem_bid = prem_mid * (1 - cfg.haircut)
                    REAL_STATS["model"] += 1
                skip = False
                if cfg.optskip:
                    prev_c = b.C[t0 + i - DAY] if i >= DAY and np.isfinite(b.C[t0 + i - DAY]) else o
                    breakout = abs(o / prev_c - 1) > 2 * sig_d
                    thin = (prem_mid / o < 0.15 * sig_d) if not use_real else (prem_mid * CV / o < 0.15 * sig_d)
                    skip = thin or breakout
                if not skip and prem_bid > 0:
                    # prem_bid is premium per underlying unit (dollars); fee per unit notional o
                    fee = min(FEE_OPT_NOTIONAL * o * units,
                              FEE_OPT_PREM_CAP * prem_bid * units) * (1 + GST)
                    net = prem_bid * units - fee
                    b.realized += net
                    b.opt = dict(K=K, units=units, call=call, i_open=i, sig=sig_p,
                                 prem_net=net,
                                 real_payoff_unit=(real["payoff"] * CV) if use_real and real.get("payoff") is not None else None)

            # 2) perp LIFO grid
            if b.cool > 0:
                b.cool -= 1
            else:
                if not b.lots and not breaker and ema_ok:
                    fill = o * (1 - b.sgn * SLIP)
                    qty = cfg.lot_frac * b.capital / fill
                    b.lots.append([fill, qty, i])
                    b.realized -= FEE_PERP_TAKER * fill * qty * (1 + GST)
                while b.lots and len(b.lots) < cfg.max_lots:
                    trig = b.lots[-1][0] * (1 - b.sgn * grid_w)
                    through = (l < trig) if b.sgn == 1 else (h > trig)
                    if not through or not ema_ok or breaker:
                        break
                    if cfg.caps:
                        cap = cfg.group_caps.get(b.group, 0.5)
                        if gnot.get(b.group, 0.0) + trig * cfg.lot_frac * b.capital > cap * capital:
                            break
                    qty = cfg.lot_frac * b.capital * (cfg.decay ** len(b.lots)) / trig
                    b.lots.append([trig, qty, i])
                    b.realized -= FEE_PERP_MAKER * trig * qty * (1 + GST)
                    if cfg.caps:
                        gnot[b.group] = gnot.get(b.group, 0.0) + trig * qty
                while b.lots and b.lots[-1][2] < i:
                    e, q, io = b.lots[-1]
                    tgt = e * (1 + b.sgn * tp_w)
                    ok = (h > tgt) if b.sgn == 1 else (l < tgt)
                    if not ok:
                        break
                    b.lots.pop()
                    pnl = (tgt - e) * b.sgn * q - FEE_PERP_MAKER * tgt * q * (1 + GST)
                    b.realized += pnl
                    b.events.append((i, ts[t0 + i], "lot", pnl))
                unreal = sum((c - e) * b.sgn * q for e, q, _ in b.lots)
                if b.lots and unreal < -cfg.stop * b.capital:
                    pnl = unreal - FEE_PERP_TAKER * c * sum(q for _, q, _ in b.lots) * (1 + GST)
                    b.realized += pnl
                    b.events.append((i, ts[t0 + i], "stop", pnl))
                    b.lots = []; b.cool = DAY
                elif cfg.max_hold_days > 0 and b.lots and (i - b.lots[-1][2]) > cfg.max_hold_days * DAY:
                    pnl = sum((c - e) * b.sgn * q for e, q, _ in b.lots) \
                        - FEE_PERP_TAKER * c * sum(q for _, q, _ in b.lots) * (1 + GST)
                    b.realized += pnl
                    b.events.append((i, ts[t0 + i], "tstop", pnl))
                    b.lots = []; b.cool = DAY

            # 3) funding on marked notional (8h)
            if cfg.funding and t.hour % 8 == 0 and b.lots:
                b.realized -= b.sgn * FUND8.get(b.sym, .0001) * sum(c * q for _, q, _ in b.lots)

            # 4) mark to market
            unreal = sum((c - e) * b.sgn * q for e, q, _ in b.lots)
            liab = 0.0
            if b.opt is not None:
                K, units, call, sig_p, io = b.opt["K"], b.opt["units"], b.opt["call"], b.opt["sig"], b.opt["i_open"]
                rem = max((DAY - (i - io)) / (365.0 * DAY), 1e-6)
                liab = bs(c, K, rem, sig_p, call) * units
            b.notional = sum(o * q for _, q, _ in b.lots) if b.lots else 0.0
            eqb[bi, i] = b.capital + b.realized + unreal - liab
            port_eq += eqb[bi, i]

        # 5) cross-margin liquidation
        maint = sum(MAINTM * b.notional for b in books)
        if port_eq < maint and maint > 0:
            worst, wu = None, 0.0
            for bi, b in enumerate(books):
                if not b.lots: continue
                c = b.C[t0 + i]
                u = sum((c - e) * b.sgn * q for e, q, _ in b.lots)
                if u < wu: worst, wu = bi, u
            if worst is not None:
                b = books[worst]; c = b.C[t0 + i]
                pnl = wu * (1 - LIQ_PEN) - FEE_PERP_TAKER * c * sum(q for _, q, _ in b.lots) * (1 + GST)
                b.realized += pnl
                b.events.append((i, ts[t0 + i], "liq", pnl))
                b.lots = []; b.cool = DAY; liq += 1
                b.notional = 0.0
                port_eq = eqb[:, i].sum()
        eq[i] = port_eq
        if port_eq <= 0:  # account ruined: equity floored at zero, trading stops
            eq[i + 1:] = 0.0
            liq += 1
            for b in books:
                b.events.append((i, ts[t0 + i], "ruin", float(port_eq)))
            t1 = t0 + i + 1
            break

    events = []
    for b in books:
        for (i, tstamp, kind, pnl) in b.events:
            events.append((tstamp, kind, pnl, b.sym, b.side))
    df_ev = pd.DataFrame(events, columns=["t", "kind", "pnl", "sym", "side"])
    ts_slice = ts[t0:t1]
    per_asset = {f"{b.sym}/{b.side}": float(eqb[bi, -1] - b.capital) for bi, b in enumerate(books)}
    return (pd.Series(eq[:len(ts_slice)], index=ts_slice), df_ev, per_asset, liq, eqb, ts_slice)


# ----------------------------------------------------------------- stats
def daily(series): return series.resample("1D").last().dropna()


def sharpe_ann(series):
    d = daily(series); r = d.pct_change().dropna()
    return float(r.mean() / r.std() * sqrt(365)) if r.std() > 0 else float("nan")


def maxdd(series):
    d = daily(series); return 100 * float((d / d.cummax() - 1).min())


def result_stats(eq, ev, capital, liq=0):
    d = daily(eq); r = d.pct_change().dropna()
    days = max((d.index[-1] - d.index[0]).days, 1)
    k = dict(pnl=float(d.iloc[-1] - capital),
             ann_pct=100 * (max(d.iloc[-1] / capital, 1e-9) ** (365 / days) - 1),
             maxdd_pct=maxdd(eq),
             sharpe=sharpe_ann(eq),
             worst_day_pct=100 * float(r.min()),
             worst_week_pct=100 * float((daily(eq) / daily(eq).shift(5) - 1).dropna().min()),
             liq=liq)
    if ev is not None and len(ev):
        pnl = ev.pnl.values
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        k.update(n=int(len(pnl)),
                 win_rate=100 * len(wins) / len(pnl),
                 avg_gain=float(wins.mean()) if len(wins) else 0.0,
                 avg_loss=float(losses.mean()) if len(losses) else 0.0,
                 rr=abs(wins.mean() / losses.mean()) if len(wins) and losses.mean() else np.nan,
                 pf=float(wins.sum() / abs(losses.sum())) if losses.sum() else np.nan,
                 biggest_loss=float(pnl.min()),
                 pnl_opt=float(ev[ev.kind == "opt"].pnl.sum()),
                 pnl_lots=float(ev[ev.kind == "lot"].pnl.sum()),
                 pnl_stops=float(ev[ev.kind.isin(["stop", "tstop", "liq"])].pnl.sum()),
                 n_opt=int((ev.kind == "opt").sum()), n_lots=int((ev.kind == "lot").sum()))
    else:
        k.update(n=0, win_rate=np.nan)
    return k


def print_row(name, k):
    print(f"{name:>22} {k['pnl']:>10,.0f} {k['ann_pct']:>8.1f} {k['maxdd_pct']:>8.1f} "
          f"{k['sharpe']:>6.2f} {(k.get('win_rate', np.nan) or np.nan):>6.1f} "
          f"{k.get('pnl_opt', 0):>10,.0f} {k.get('pnl_lots', 0):>10,.0f} {k.get('pnl_stops', 0):>9,.0f}")


# ----------------------------------------------------------------- helpers
def buyhold(rows, capital):
    tw = sum(r["wt"] for r in rows)
    print("\nBuy & hold, same capital weights:")
    tot = 0.0
    for r in rows:
        w = capital * r["wt"] / tw
        i0 = first_finite(r["C"])
        if i0 is None:
            print(f"  {r['sym']}/{r['side']:<6}: no data"); continue
        c0, c1 = r["C"][i0], r["C"][-1]
        w_eff = w * (len(r["C"]) - i0) / len(r["C"])   # fraction of period traded in-market
        pnl = w_eff * (c1 / c0 - 1)
        tot += pnl
        print(f"  {r['sym']}/{r['side']:<6}: {100*(c1/c0-1):+7.2f}%  pnl {pnl:+10,.0f} (in-mkt frac {w_eff/w:.2f})")
    print(f"  B&H weighted pnl {tot:+,.0f} on {capital:,.0f}")


def deflated_sharpe(sr_daily, trial_daily_sharpes):
    """Bailey/Lopez de Prado expected-max haircut (daily units). Returns DSR magnitude."""
    if not len(trial_daily_sharpes):
        return float("nan")
    from statistics import NormalDist
    N = max(len(trial_daily_sharpes), 2)
    v = float(np.var(trial_daily_sharpes))
    if v <= 0: return float("nan")
    nd = NormalDist()
    e_max = sqrt(v) * ((1 - 0.5772156649) * nd.inv_cdf(1 - 1.0 / N)
                       + 0.5772156649 * nd.inv_cdf(1 - 1.0 / (N * math.e)))
    return sr_daily - e_max


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-05-09")
    ap.add_argument("--end", default="2026-10-23")
    ap.add_argument("--capital", type=float, default=10000)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--walkforward", action="store_true")
    ap.add_argument("--option-mode", choices=["model", "mixed"], default="mixed",
                    help="mixed: use collected REAL bids/settlements where days exist")
    args = ap.parse_args()

    start = pd.Timestamp(args.start, tz="UTC"); end = pd.Timestamp(args.end, tz="UTC")
    global REAL_OPTS
    global REAL_OPTS
    REAL_OPTS = load_real_options() if args.option_mode == "mixed" else {}
    if args.option_mode == "mixed":
        n_real = sum(1 for v in REAL_OPTS.values() if v.get("payoff") is not None)
        print(f"option-mode=mixed: {len(REAL_OPTS)} collected entry quotes, "
              f"{n_real} with settlements from collector.db")
    else:
        print("option-mode=model (BS with iv_mult)")
    print(f"Loading data for {len(ASSETS)} assets {start.date()} -> {end.date()} "
          f"{'(SYNTHETIC)' if args.synthetic else '(real API)'} ...")
    bysym = {}
    for sym, sides, w, opt, grp in ASSETS:
        if sym in bysym: continue
        try:
            df, vol_d, ema = load_arrays(sym, start, end, args.synthetic)
            bysym[sym] = (df, vol_d.values, ema.values, grp)
        except Exception as ex:
            print(f"  skip {sym}: {ex}")
            bysym[sym] = None
    rows = []
    union = None
    for sym, sides, w, opt, grp in ASSETS:
        if bysym.get(sym) is None: continue
        union = bysym[sym][0].index if union is None else union.union(bysym[sym][0].index)
    n_missing = 0
    for sym, sides, w, opt, grp in ASSETS:
        if bysym.get(sym) is None: continue
        df, V, E, _ = bysym[sym]
        D = df.reindex(union)
        V2 = pd.Series(V, index=df.index).reindex(union).values
        E2 = pd.Series(E, index=df.index).reindex(union).values
        n_missing += int(D.close.isna().sum())
        for side in sides:
            rows.append(dict(sym=sym, side=side, wt=w / len(sides), opt=opt, grp=grp,
                             O=D.open.values, H=D.high.values, L=D.low.values,
                             C=D.close.values, V=V2, E=E2))
    ts = union
    print(f"(missing cross-symbol hours: {n_missing})")
    books = make_books(rows, args.capital)
    print(f"books={len(books)}  bars={len(ts)}  {ts[0]} -> {ts[-1]}")

    results = {}
    if not args.walkforward:
        print("\n%-22s %10s %8s %8s %6s %6s %10s %10s %9s" %
              ("config", "pnl$", "ann%", "maxDD%", "sharpe", "wr%", "opt$", "lots$", "stops$"))
        for name, cfg in ABLATIONS:
            if args.option_mode == "mixed":
                cfg.opt_mode = "mixed"
            eq, ev, _, liq, _, _ = simulate(make_books(rows, args.capital), ts, cfg, args.capital)
            k = result_stats(eq, ev, args.capital, liq)
            results[name] = k
            print_row(name, k)
        final_run_cfg = replace(FINAL, opt_mode=args.option_mode)
        eq_f, ev_f, pa, liq_f, _, _ = simulate(make_books(rows, args.capital), ts, final_run_cfg, args.capital)
        print(f"\nFINAL per-book pnl (liq={liq_f}):")
        for sym_, v in sorted(pa.items(), key=lambda kv: -abs(kv[1])):
            print(f"  {sym_:>15}: {v:+10,.0f}")
        rx = REAL_STATS
        tot_r = rx["real"] + rx["model"]
        print(f"\noption pricing source: REAL collected bids {rx['real']}/{tot_r} "
              f"({100*rx['real']/max(tot_r,1):.0f}%), modelled {rx['model']}/{tot_r}; "
              f"payoffs real/model {rx['real_payoff']}/{rx['model_payoff']}"
              if tot_r else "\noption pricing source: n/a")
        if len(ev_f):
            print("\nevent kinds:\n" + str(ev_f.groupby("kind").pnl.agg(["count", "sum"])))
            pd.DataFrame(ev_f).to_csv(f"{os.path.dirname(os.path.abspath(__file__))}/events_v2.csv", index=False)
        pd.DataFrame(pd.Series(eq_f.values, index=pd.DatetimeIndex(ts))).to_csv("equity_v2.csv")
        json.dump(results, open(f"{os.path.dirname(os.path.abspath(__file__))}/results_ablation.json", "w"),
                  indent=1, default=str)
        buyhold(rows, args.capital)
        return

    # ---------------------------- walk-forward ----------------------------
    print("\n=== WALK-FORWARD  (IS 60d tune -> OOS 30d eval, step 30d) ===")
    grid = [dict(k=k, rat=r, max_hold_days=h) for k in (0.8, 1.2, 1.6)
            for r in (0.4, 0.6, 0.8) for h in (2.0, 3.0)]
    t0, t1 = int(WARMUP), len(ts)
    IS, OO, STEP = 60, 30, 30
    segs = []
    pos = t0
    while pos + (IS + OO) * DAY <= t1:
        segs.append((pos, pos + IS * DAY, pos + (IS + OO) * DAY))
        pos += STEP * DAY
    print(f"segments={len(segs)}  grid configs={len(grid)}  trials={len(segs)*len(grid)}")
    oos_eqs, trials_sh, chosen, oos_stats = [], [], [], []
    for (a, b, c) in segs:
        best, best_sh = None, -9e9
        for pg in grid:
            eq_i, _, _, _, _, _ = simulate(make_books(rows, args.capital), ts,
                                           replace(FINAL, **pg), args.capital,
                                           ts[a], ts[b - 1])
            sh = float(sharpe_ann(eq_i))
            trials_sh.append(sh)
            if np.isfinite(sh) and sh > best_sh:
                best_sh, best = sh, pg
        eq_o, ev_o, _, liq, _, tss = simulate(make_books(rows, args.capital), ts,
                                              replace(FINAL, **best), args.capital,
                                              ts[b], ts[c - 1])
        k = result_stats(eq_o, ev_o, args.capital, liq)
        oos_stats.append(k)
        oos_eqs.append(eq_o.values[1:])
        chosen.append(best)
        print(f"  IS {ts[a].date()}->{ts[b-1].date()} shp {best_sh:5.2f} chose {best} -> "
              f"OOS pnl {k['pnl']:+8,.0f} DD {k['maxdd_pct']:6.1f}% shp {k['sharpe']:>5.2f}")
    oos_all = pd.Series(np.concatenate(oos_eqs))
    oos_index = pd.date_range(pd.Timestamp(ts[t0]) + pd.Timedelta(days=IS), periods=len(oos_all), freq="1h")
    oos_all.index = oos_index
    oos_all = oos_all / oos_all.iloc[0] * args.capital
    sh_oos = sharpe_ann(oos_all)
    dsr = deflated_sharpe(sh_oos / sqrt(365), [s / sqrt(365) for s in trials_sh])
    d = daily(oos_all); rr = d.pct_change().dropna()
    print(f"\nOOS aggregate ({len(oos_eqs)} segments):")
    print(f"  pnl  {d.iloc[-1] - args.capital:+,.0f}")
    print(f"  ann  {100*((d.iloc[-1]/args.capital)**(365/((d.index[-1]-d.index[0]).days))-1):+.1f}%")
    print(f"  maxDD {maxdd(oos_all):.1f}%   sharpe {sh_oos:.2f}")
    print(f"  worst day {100*rr.min():.2f}%   worst week {100*(d/d.shift(5)-1).dropna().min():.2f}%")
    print(f"  deflated Sharpe penalty over {len(trials_sh)} trials: "
          f"OOS daily SR {sh_oos/sqrt(365):.4f} vs expected-max-haircut DSR={dsr:.4f} "
          f"=> {'EDGE PLAUSIBLE' if dsr > 0 else 'NO EDGE survives multiple-testing'}")

    # parameter stability (mean IS-sharpe of neighbours on first segment)
    nb_ok = []
    for pg in chosen:
        k, r_, h = pg["k"], pg["rat"], pg["max_hold_days"]
        neighs = []
        for dk, dr, dh in ((.4, 0, 0), (-.4, 0, 0), (0, .2, 0), (0, -.2, 0), (0, 0, 1.0)):
            eq_n = simulate(make_books(rows, args.capital), ts,
                            replace(FINAL, k=k + dk, rat=r_ + dr, max_hold_days=h + dh),
                            args.capital, ts[segs[0][0]], ts[segs[0][1] - 1])[0]
            neighs.append(sharpe_ann(eq_n))
        f = [x for x in neighs if np.isfinite(x)]
        best = max(f) if f else 1.0
        nb_ok.append(sum(1 for x in f if x >= 0.3 * best) / len(neighs))
    print(f"  parameter plateau score (mean share of neighbouring configs retaining >30% of IS sharpe): "
          f"{np.mean(nb_ok):.2f}")

    # regime split on OOS: bucket each segment pnl by BTC 10d return at OOS start
    bret = pd.Series(bysym["BTCUSD"][0].close.values, index=ts).pct_change(10)
    buckets = {"up": [], "down": [], "flat": []}
    for si, (a, b, _) in enumerate(segs):
        val = bret.get(ts[b], np.nan)
        bucket = ("up" if val > 0.02 else "down" if val < -0.02 else "flat") \
            if np.isfinite(val) else "flat"
        buckets[bucket].append(oos_stats[si]["pnl"])
    print("\n  OOS pnl by BTC 10d-regime at evaluation start:")
    for kk, vv in buckets.items():
        print(f"    {kk:>5}: {[f'{x:+,.0f}' for x in vv]}  total {sum(vv):+,.0f}")

    # worst single day: BTC crash day
    bret2 = pd.Series(bysym["BTCUSD"][0].close.values, index=ts).pct_change()
    crashday = pd.Timestamp(bret2.idxmin())
    eq_c, ev_c, _, _, _, _ = simulate(make_books(rows, args.capital), ts, FINAL, args.capital,
                                      crashday - pd.Timedelta(days=2), crashday + pd.Timedelta(days=1))
    print(f"  BTC crash day {crashday.date()} (day of max BTC loss): portfolio pnl from "
          f"[-2d] to [+1d]: {eq_c.iloc[-1] - args.capital:+,.0f} (capital {args.capital:,.0f})")

    # Monte-Carlo shuffle of OOS daily returns (drawdown distribution)
    rl = daily(oos_all).pct_change().dropna().values
    rngg = np.random.default_rng(1)
    dds = []
    for _ in range(2000):
        s = 1.0; pk = 1.0; mdd = 0.0
        for x in rl[rngg.permutation(len(rl))]:
            s *= (1 + x); pk = max(pk, s); mdd = min(mdd, s / pk - 1)
        dds.append(mdd)
    print(f"  MC shuffle maxDD: p5 {100*np.percentile(dds,5):.1f}%  med {100*np.median(dds):.1f}%  "
          f"p95 {100*np.percentile(dds,95):.1f}%  vs realised path maxDD {maxdd(oos_all):.1f}%")

    json.dump(dict(oos_stats=oos_stats, chosen=chosen, trials=len(trials_sh),
                   oos_sharpe=sh_oos, dsr=dsr, mc_dd=[float(np.percentile(dds, 5)),
                                                      float(np.median(dds)), float(np.percentile(dds, 95))]),
              open(f"{os.path.dirname(os.path.abspath(__file__))}/results_walkforward.json", "w"),
              indent=1, default=str)
    print("  wrote results_walkforward.json")


if __name__ == "__main__":
    main()
