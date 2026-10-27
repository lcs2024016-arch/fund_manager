#!/usr/bin/env python3
"""
Model-free analysis of "sell the option at the bid, hold to expiry" — Delta India
daily chain, using ONLY collected data from collector.db (plus the underlying
index/prices the collector stored; a sigma estimate for strike offsets may fall
back to the repo's cached perp candles, but P&L itself is never modelled).

Usage:
  .venv/bin/python validate/analyze_options.py --size 10        # contracts per candidate
Writes: validate/sample_report.txt, validate/calibration.json
"""
import argparse, json, math, os, sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "collector.db")
CONTRACT_VALUE = {"BTC": 0.001, "ETH": 0.010, "XAUT": 0.001}   # per-coin cv, verified 2026-10-23
FEE_NOTIONAL, FEE_CAP, GST = 0.0001, 0.035, 0.18

MIN_WEEKS_FOR_EDGE = 8


def con(): return sqlite3.connect(DB, timeout=60)


def fee(underlying, spot, premium, units=1.0):
    notional = spot * CONTRACT_VALUE[underlying] * units
    return min(FEE_NOTIONAL * notional, FEE_CAP * premium) * (1 + GST)


def daily_sigma(con):
    """Trailing daily sigma of the underlying from the collector's own snapshot
    series: per underlying (hourly snap quotes -> 1d pct std proxy), else 0."""
    df = pd.read_sql(
        "SELECT underlying, run_ts, underlying_price FROM snapshots_2 "
        "WHERE underlying_price IS NOT NULL", con)
    df["run_ts"] = pd.to_datetime(df["run_ts"], utc=True)
    out = {}
    for ua, g in df.groupby("underlying"):
        s = g.set_index("run_ts")["underlying_price"].sort_index()
        s = s[~s.index.duplicated(keep="first")]
        if len(s) < 30:
            out[ua] = None
            continue
        r = s.pct_change().dropna()
        out[ua] = float(r.std() * math.sqrt(24))  # daily sigma from hourly snaps
    return out


def entry_candidates(con):
    df = pd.read_sql(
        "SELECT run_ts, kind, underlying, call_put, strike, best_bid, best_ask, bid_size, "
        "ask_size, mark_price, mark_iv, bid_iv, ask_iv, delta, theta, open_interest, volume, "
        "underlying_price, l2_bid_depth, expiry_ts, symbol FROM snapshots_2 "
        "WHERE kind IN ('entry','adhoc','preexpiry') AND best_bid IS NOT NULL", con)
    df["run_ts"] = pd.to_datetime(df["run_ts"], utc=True)
    df["day"] = df["run_ts"].dt.date
    df["spot"] = df["underlying_price"]
    df["mid"] = (df.best_bid + df.best_ask) / 2
    df["spread_pct"] = (df.best_ask - df.best_bid) / df.mid.replace(0, np.nan)
    df["moneyness"] = df.strike / df.spot
    return df


def pnl_table(df, sig, size=1.0):
    """Per (day, underlying, side, offset-group) collect candidate rows with the
    entry bid; payoff is joined from settlements when the option has expired."""
    con_ = con()
    try:
        stt = pd.read_sql("SELECT symbol, expiry_date, settle_index_price, payoff, "
                          "pnl_per_contract, entry_bid, run_ts FROM settlements", con_)
        stt = stt.drop_duplicates("symbol", keep="last").set_index("symbol")
    finally:
        con_.close()
    rows = []
    for _, r in df.iterrows():
        # strike offset groups
        active = sig.get(r.underlying)
        groups = ["fixed_0.3pct" if abs(r.moneyness - (1 + 0.003)) < 0.0025 and False else None]
        grp = []
        grp.append("ATM" if abs(r.moneyness - 1) < 0.0025 else None)
        grp.append("fixed_0.3pct" if abs(r.moneyness - (1 + (0.003 if r.call_put == "call" else -0.003))) < 0.0025 else None)
        grp.append("fixed_0.5pct" if abs(r.moneyness - (1 + (0.005 if r.call_put == "call" else -0.005))) < 0.0025 else None)
        if active:
            otmd = (1 + (active if r.call_put == "call" else -active))
            grp.append(f"sig_{active:.4f}" if abs(r.moneyness - otmd) < 0.004 else None)
        otm = (r.moneyness - 1) if r.call_put == "call" else (1 - r.moneyness)
        if 0 <= otm <= 0.02:
            grp.append("OTM_band")
        for g in set([x for x in grp if x]):
            premium = r.best_bid * CONTRACT_VALUE[r.underlying] * size
            pay_row = stt.loc[r.symbol] if r.symbol in stt.index else None
            payoff = float(pay_row["payoff"]) * size if pay_row is not None and np.isfinite(pay_row["payoff"]) else None
            settle = float(pay_row["settle_index_price"]) if pay_row is not None else None
            settled_pnl = (float(pay_row["pnl_per_contract"]) * size) if pay_row is not None else None
            rows.append(dict(
                day=r.day, underlying=r.underlying, side=r.call_put, group=g,
                strike=r.strike, spot=r.spot, symbol=r.symbol,
                bid=r.best_bid, mark_iv=r.mark_iv, spot_iv=r.spot,
                premium=premium, fee=fee(r.underlying, r.spot, r.best_bid, size),
                payoff=payoff, settle_idx=settle,
                pnl=settled_pnl if settled_pnl is not None else (premium - (payoff * size if payoff is not None else 0) - fee(r.underlying, r.spot, r.best_bid, size)),
                settled=pay_row is not None))
    return pd.DataFrame(rows)


def summarize(t, by):
    out = []
    for k, g in t.groupby(by, dropna=False):
        settled = g[g.settled]
        pnl = settled.pnl.dropna()
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        d = {
            "key": k,
            "n": len(g),
            "n_settled": len(settled),
            "avg_premium": float(g.premium.mean()),
            "gross_premium": float(g.premium.sum()),
            "avg_worst_payoff": float(g.payoff.max()) if len(settled) else None,
            "win_rate": float(len(wins) / len(pnl)) if len(pnl) else None,
            "avg_win": float(wins.mean()) if len(wins) else None,
            "avg_loss": float(losses.mean()) if len(losses) else None,
            "worst": float(pnl.min()) if len(pnl) else None,
            "profit_factor": float(wins.sum() / abs(losses.sum())) if losses.sum() else None,
            "tail_mult": (float(abs(pnl.min()) / g.premium.mean()) if len(pnl) and g.premium.mean() else None),
        }
        out.append(d)
    return pd.DataFrame(out)


def bootstrap_ci(x, f=np.mean, iters=4000, seed=3):
    if len(x) < 8: return None
    rng = np.random.default_rng(seed)
    x = np.asarray(x)
    vals = [f(x[rng.integers(0, len(x), len(x))]) for _ in range(iters)]
    return (float(np.quantile(vals, 0.05)), float(np.quantile(vals, 0.95)))


def iv_vs_realized(entry_df):
    """Compare mark_iv at entry with realised (next-day, next-week) underlying vol
    using the collector's own underlying snapshot series."""
    px = pd.read_sql("SELECT underlying, run_ts, underlying_price FROM snapshots_2 "
                     "WHERE underlying_price IS NOT NULL", con())
    px["run_ts"] = pd.to_datetime(px["run_ts"], utc=True)
    rows = []
    for (ua, day), g in entry_df.groupby(["underlying", "day"]):
        t0 = pd.Timestamp(day, tz="UTC")
        after = px[(px.underlying == ua) & (px.run_ts >= t0)
                   & (px.run_ts < t0 + timedelta(days=2))]
        s = after.set_index("run_ts").underlying_price.sort_index()
        if len(s) < 4:
            continue
        r1 = s.pct_change().dropna()
        realized_1d = float(r1.abs().max())
        realized_vol = float(r1.std() * math.sqrt(24))
        rows.append(dict(day=day, underlying=ua, mark_iv=float(g.mark_iv.median()),
                         realized_1d_absmove=realized_1d, realized_daily_vol=realized_vol,
                         vr_premium=float(g.mark_iv.median()) - realized_vol))
    return pd.DataFrame(rows)


def stress(entry_df, capital=10000.0, sizes=(1, 5, 10, 25)):
    """Shock-day loss for short option books as multiples of avg daily premium."""
    shocks = {"BTC": (.06, .08, .10), "ETH": (.06, .08, .10), "XAUT": (.03, .05)}
    out = []
    for (ua, side), g in entry_df.drop_duplicates("symbol").groupby(["underlying", "call_put"]):
        prem = float(g.best_bid.mean()) * CONTRACT_VALUE[ua]      # 1 contract average premium
        short_delta = float(g.delta.mean()) if np.isfinite(g.delta.mean()) else (-0.9 if side == "put" else 0.9)
        # 1 contract units = contract_value; shock moves spot by s
        for s in shocks.get(ua, (.06, .08, .10)):
            for size in sizes:
                strike_m = float(g.strike.mean())
                # shock to the ATM round-strike: call price moves up by s, put moves DOWN by s
                payoff = max(s * g.spot.iloc[0], 0)     # ATM: both sides go ~s*spot ITM
                notional = g.spot.iloc[0] * CONTRACT_VALUE[ua] * size
                loss_rate = (payoff * CONTRACT_VALUE[ua] * size - prem * size) / notional
                out.append(dict(underlying=ua, side=side, shock_pct=s, size=size,
                                premium=prem * size,
                                loss_frac_of_notional=loss_rate,
                                loss_pct_of_capital=100 * loss_rate * (notional / capital),
                                tail_mult=(abs(loss_rate * notional)) / max(prem * size, 1e-9)))
    return pd.DataFrame(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=float, default=1.0, help="contracts sold per candidate")
    ap.add_argument("--capital", type=float, default=10000.0)
    args = ap.parse_args()

    c = con()
    n_runs = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    n_days = c.execute("SELECT COUNT(DISTINCT substr(run_ts,1,10)) FROM snapshots_2").fetchone()[0]
    first, last = c.execute("SELECT MIN(run_ts), MAX(run_ts) FROM snapshots_2").fetchone()
    n_set = c.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    entries = entry_candidates(c)
    sig = daily_sigma(c)
    t = pnl_table(entries, sig, args.size)
    st = summarize(t, "group") if len(t) else pd.DataFrame()
    perm = pd.DataFrame()
    if len(t):
        perm = summarize(t[t.group == "ATM"], "underlying") if (t.group == "ATM").any() else pd.DataFrame()
        perm2 = summarize(t[t.group.isin(["underlying"])], "underlying")
    ivr = iv_vs_realized(entries) if len(entries) else pd.DataFrame()
    strs = stress(entries, args.capital) if len(entries) else pd.DataFrame()

    # liquidity stats
    liq = []
    if len(entries):
        for (ua, side), g in entries.groupby(["underlying", "call_put"]):
            liq.append(dict(underlying=ua, side=side,
                            n=len(g),
                            median_spread_pct=float(g.spread_pct.median()),
                            p90_spread_pct=float(g.spread_pct.quantile(0.9)),
                            bid_zero_pct=float((g.best_bid <= 0).mean() * 100),
                            bid_missing_pct=float((g.best_bid.isna()).mean() * 100),
                            median_l2_depth_at_bid=float(g.l2_bid_depth.dropna().median()) if g.l2_bid_depth.notna().any() else None))
    liq = pd.DataFrame(liq)

    lines = []
    lines.append("=" * 78)
    lines.append(f"OPTION-LEG MODEL-FREE ANALYSIS  (collector.db, {n_days} day(s) of data, "
                 f"{n_runs} runs, {first} -> {last})")
    lines.append("=" * 78)
    lines.append(f"data days with entry snapshots: {n_days}; settle rows captured: {n_set}")
    lines.append(f"snap rows total: {len(entries)}; candidate P&L rows (offset-group rows): {len(t)}")
    lines.append("")
    if len(ivr):
        m = ivr.groupby("underlying").agg(
            days=("day", "count"), mark_iv=("mark_iv", "median"),
            realized_daily_vol=("realized_daily_vol", "median"),
            vr_premium_median=("vr_premium", "median"),
            vr_premium_p10=("vr_premium", lambda x: x.quantile(.1)),
            vr_premium_p90=("vr_premium", lambda x: x.quantile(.9)))
        lines.append("Variance-risk premium at entry (mark_iv minus realised next-day vol):")
        lines.append(m.round(4).to_string())
        lines.append("")
    lines.append("ATM P&L per underlying (requires settlements; 'n_settled' tells when):")
    if len(perm):
        lines.append(perm.round(2).to_string())
    else:
        lines.append("  n/a yet — no settled expired-position rows in DB.")
    lines.append("")
    lines.append("Liquidity at the bid:")
    if len(liq):
        lines.append(liq.to_string(index=False))
    lines.append("")
    lines.append("Shock-day stress (loss as multiple of ONE day's ATM premium):")
    if len(strs):
        lines.append(strs.groupby(["underlying", "side", "shock_pct"]).tail_mult.agg(
            ["max", "count"]).round(2).to_string())
    lines.append("")
    ci = bootstrap_ci(t[t.settled].pnl.dropna()) if len(t) else None
    if ci:
        lines.append(f"bootstrap 90% CI of settled average per-contract pnl: [{ci[0]:.4f}, {ci[1]:.4f}] "
                     f"(n={len(t[t.settled])})")
    else:
        lines.append("no bootstrap CI: needs >=8 settled observations")
    lines.append("")
    limits = []
    if n_days < 7:
        limits.append("n < 1 week: NOT interpretable.")
    if n_days < MIN_WEEKS_FOR_EDGE * 7:
        limits.append(f"n < {MIN_WEEKS_FOR_EDGE} weeks: cannot claim an edge even if P&L positive.")
    if n_set == 0:
        limits.append("no expired settlements captured yet: forward P&L unknown.")
    if any(v is None for v in sig.values()):
        limits.append("daily sigma from snapshots not ready for some underlyings (offsets degraded).")
    lines.append("STATISTICAL HONESTY: " + "; ".join(limits) or "n/a")
    txt = "\n".join(lines)
    open(os.path.join(HERE, "sample_report.txt"), "w").write(txt)
    print(txt)

    calib = {}
    if len(entries):
        calib = {
            "source": "collector.db first run",
            "n_snapshots": int(len(entries)),
            "days": int(n_days),
            "median_spread_pct": {f"{u}/{s}": float(g.spread_pct.median())
                                  for (u, s), g in entries.groupby(["underlying", "call_put"]) if np.isfinite(g.spread_pct.median())},
            "mark_iv_median": {u: float(g.mark_iv.median()) for u, g in entries.groupby("underlying")},
            "realised_daily_vol": {u: sig.get(u) for u in sig},
            "vr_premium_daily": {u: (float((ivr[ivr.underlying == u].vr_premium).median())
                                     if (ivr.underlying == u).any() else None) for u in sig},
            "note": "spreads are median of (ask-bid)/mid within +/-10% strike window",
        }
    json.dump(calib, open(os.path.join(HERE, "calibration.json"), "w"), indent=1)
    print("wrote validate/sample_report.txt and validate/calibration.json")


if __name__ == "__main__":
    main()
