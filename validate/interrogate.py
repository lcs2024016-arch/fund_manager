#!/usr/bin/env python3
"""
interrogate.py — Stage 3: interrogate the leader record for luck, survivorship
and hidden risk. Reads leader_trades.db (populated from the user's HAR export).
Every question is answered with numbers from the DB, or prints STAGE1 DATA
MISSING. No invented data.

Q1 contribution to profit: top-5 days, top-20 trades, best month, Oct25-Jun26.
Q2 realised vs mark-to-market PnL, net of fees.
Q3 Jun/Jul-2026 drawdown decomposition by asset/leg/strike.
Q4 monthly distribution: skew/kurtosis/worst day/week/underwater/ulcer.
Q5 bootstrap CI on mean daily return + Sharpe, t-stat, deflated Sharpe.
Q6 follower return vs leader (from follower_history + slippage assumption).
Q7 survivorship ranking proxy (needs platform-wide leaderboard export).
"""
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "leader_trades.db")
N_BOOT = 5000
N_TRIALS = 400  # leaders one might have screened while picking this one


def daily_equity(conn):
    return [dict(r) for r in conn.execute(
        "SELECT ts, equity, pnl, realized, unrealized FROM daily_equity ORDER BY ts")]


def stats_monthly(daily):
    monthly = defaultdict(float)
    for d in daily:
        monthly[d["ts"][:7]] += d["pnl"] or 0
    return monthly


def skew_kurt(xs):
    n = len(xs)
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / n or 1e-12
    s = sum((x - m) ** 3 for x in xs) / n / v ** 1.5
    k = sum((x - m) ** 4 for x in xs) / n / v ** 2 - 3
    return round(s, 3), round(k, 3)


def ulcer(equity):
    peak, sq = equity[0], 0.0
    for x in equity:
        peak = max(peak, x)
        r = 100 * (1 - x / max(peak, 1e-9)) if isinstance(x, (int, float)) else 0
        sq += r ** 2
    return round(math.sqrt(sq / len(equity)), 3)


def underwater(equity):
    peak, days, best = equity[0], 0, 0
    for x in equity:
        if x >= peak:
            peak, days = x, 0
        else:
            days += 1
            best = max(best, days)
    return best


def bootstrap_daily(rets):
    n = len(rets)
    bs, bs_sh = [], []
    import random
    rng = random.Random(7)
    for _ in range(N_BOOT):
        sample = [rets[rng.randrange(n)] for _ in range(n)]
        mu = sum(sample) / n
        sd = math.sqrt(sum((x - mu) ** 2 for x in sample) / (n - 1) or 1e-18)
        bs.append(mu)
        bs_sh.append(mu / sd if sd else 0)  # daily Sharpe; annualised elsewhere
    bs.sort(); bs_sh.sort()
    q = lambda a: a[int(.025 * len(a))], None
    lo, hi = bs[int(.025 * len(bs))], bs[int(.975 * len(bs))]
    slo, shi = bs_sh[int(.025 * len(bs_sh))], bs_sh[int(.975 * len(bs_sh))]
    mean = sum(bs) / len(bs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in bs) / len(bs) or 1e-18)
    t = mean / (sd / math.sqrt(len(bs)))
    return {"mean_daily_ret": mean, "t_stat": t,
            "CI_mean": [round(lo, 6), round(hi, 6)],
            "CI_sharpe": [round(slo, 3), round(shi, 3)]}


def deflated_sharpe(sr_daily, n, trials=N_TRIALS):
    """Bailey & LdP: expected max Sharpe across `trials` configs vs observed."""
    from math import sqrt, exp, pi, log
    # expected max of N(0,1)-ish trials over eff. sqrt(periods)
    emax = sqrt(2 * log(trials * (2 * pi ** 2)))  # rough first-order
    e = emax / sqrt(max(1, n))
    sr_ann = sr_daily * sqrt(252)
    dsr = sr_ann - e
    return sr_ann, round(e, 4), round(dsr, 3)  # observed, haircut, net


def main():
    if not os.path.exists(DB):
        print("STAGE1 DATA MISSING"); return 2
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    daily = daily_equity(conn)
    if not daily:
        print("STAGE1 DATA MISSING — no daily_equity rows yet.")
        print("Run har_ingest.py on the user's HAR export first.")
        return 2
    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE exit_ts != ''")]
    R = {}

    # Q1 contribution
    daily_pnl = [d["pnl"] or 0 for d in daily]
    top5 = sorted(daily_pnl, reverse=True)[:5]
    top20t = sorted(trades, key=lambda t: t["realized_pnl"] or 0, reverse=True)[:20]
    tot = sum(daily_pnl) or 1
    monthly = stats_monthly(daily)
    R["Q1"] = {
        "total_pnl": tot,
        "top5_days_pnl": round(sum(top5), 2),
        "top5_days_share": round(sum(top5) / tot, 4),
        "top20_trades_pnl": round(sum(t["realized_pnl"] or 0 for t in top20t), 2),
        "top20_trades_share_of_trade_pnl": round(
            sum(t["realized_pnl"] or 0 for t in top20t)
            / (sum(t["realized_pnl"] or 0 for t in trades) or 1), 4),
        "best_month": max(monthly, key=monthly.get),
        "best_month_pnl": round(max(monthly.values()), 2),
        "excl_best_month_pnl": round(tot - max(monthly.values()), 2),
    }

    # Q2 realised vs MTM
    realized_sum = sum((t["realized_pnl"] or 0) for t in trades)
    fee_sum = sum((t["fees"] or 0) for t in trades)
    open_unreal = sum((r[0] or 0) for r in conn.execute(
        "SELECT unrealized_pnl FROM open_positions"))
    R["Q2"] = {
        "realized_pnl_sum": round(realized_sum, 2),
        "fees_sum": round(fee_sum, 2),
        "open_unrealized_now": round(open_unreal, 2),
        "mark_to_market_now": round(realized_sum + open_unreal, 2),
        "note": "funding not itemised in fills; treat as unknown unless exports include it"
    }

    # Q3 Jun/Jul-2026 drawdown
    dd_daily = defaultdict(float)
    for t in trades:
        ex = str(t["exit_ts"])
        if ex[:7] in ("2026-06", "2026-07"):
            dd_daily[ex[:10] + "|" + t["symbol"] + "|" + str(t["instrument"])] += \
                (t["realized_pnl"] or 0)
    worst = sorted(dd_daily.items(), key=lambda kv: kv[1])[:20]
    daily_grad = [(d["ts"][:10], d["pnl"] or 0) for d in daily
                  if d["ts"][:7] in ("2026-06", "2026-07")]
    nav = 0
    trough_day, trough_val = None, 0
    for ts, p in daily_grad:
        nav += p
        if nav < trough_val:
            trough_val, trough_day = nav, ts
    avg_abs_daily = sum(abs(p) for _, p in daily_grad) / max(1, len(daily_grad)) or 1
    R["Q3"] = {
        "worst_symbol_day_legs": [[k, round(v, 2)] for k, v in worst],
        "trough_up_to": trough_day,
        "trough_cum_pnl": round(trough_val, 2),
        "loss_as_multiple_of_avg_daily_abs_pnl": round(trough_val / avg_abs_daily, 2),
        "note": "as % of $1000 margin: divide trough_cum_pnl by 10"
    }

    # Q4 distribution (distribution metrics over the ACTIVE trading window,
    # Sep 2025 onward, since the leader was near-dormant 2024-09..2025-08)
    daily_act = [d for d in daily if (d["ts"] or "")[:7] >= "2025-09"]
    mret = list(monthly.values())
    eq = [d["equity"] for d in daily_act if d["equity"]]
    wk = defaultdict(float)
    for d in daily_act:
        wk[d["ts"][:8]] += d["pnl"] or 0  # iso week approx by 8-char prefix
    sp, ku = skew_kurt(mret)
    R["Q4"] = {
        "n_months": len(mret), "skew_monthly": sp, "excess_kurt": ku,
        "worst_day": min((d["pnl"] or 0) for d in daily_act),
        "worst_week": min(wk.values()),
        "longest_underwater_days": underwater(eq),
        "ulcer_index": ulcer(eq),
        "note": "short-vol signature = positive skew + mild kurtosis + deep sustained DD"
    }

    # Q5 significance
    d0 = daily[0]["ts"][:4] if daily else ""
    # Q5 significance — % returns on the fixed $1,000 recommended margin
    CAPITAL = 1000.0
    # zero-fill calendar days so dormant days aren't dropped
    cal = defaultdict(float)
    for d in daily:
        cal[(d["ts"] or "")[:10]] += d["pnl"] or 0
    days_all = sorted(cal)
    first, last = days_all[0], days_all[-1]
    from datetime import datetime as _dt, timedelta as _td
    d0 = _dt.strptime(first, "%Y-%m-%d")
    d1 = _dt.strptime(last, "%Y-%m-%d")
    zero_ret = []
    n_active = 0
    cur = d0
    while cur <= d1:
        k = cur.strftime("%Y-%m-%d")
        p = cal.get(k, 0.0)
        if cal.get(k):
            n_active += 1
        zero_ret.append(p / CAPITAL)
        cur += _td(days=1)
    rets = [((d["pnl"] or 0) / CAPITAL) for d in daily]
    rets_calendar = zero_ret
    boot = bootstrap_daily(rets_calendar)
    bootv2 = bootstrap_daily(rets)  # trade-days only
    sd = math.sqrt(sum((x - rets[len(rets)//2]) ** 2 for x in rets) / max(1, len(rets) - 1) or 1e-12)
    mean = sum(rets) / len(rets)
    sr_d = mean / (math.sqrt(sum((x - mean) ** 2 for x in rets) / (len(rets) - 1) or 1e-18))
    sr_d_cal = sum(rets_calendar) / len(rets_calendar) / (
        math.sqrt(sum((x - sum(rets_calendar)/len(rets_calendar)) ** 2 for x in rets_calendar)
                  / (len(rets_calendar) - 1) or 1e-18))
    boot["sharpe_daily_trade_days"] = round(sr_d, 4)
    boot["sharpe_daily_calendar"] = round(sr_d_cal, 4)
    boot["n_trade_days"] = len(rets)
    boot["n_calendar_days"] = len(rets_calendar)
    boot["n_active_days"] = n_active
    boot["fraction_days_trading"] = round(n_active / len(rets_calendar), 3)
    obs, haircut, net = deflated_sharpe(sr_d_cal, len(rets_calendar))
    obs2, haircut2, net2 = deflated_sharpe(sr_d, len(rets))
    boot["deflated_sharpe"] = {
        "calendar_daily_sr": round(sr_d_cal, 4),
        "annualised": round(obs, 3), "expected_max_haircut": haircut,
        "net": net, "pass": net > 0,
        "trade_days_variant_net": net2}
    boot["deflated_sharpe"] = {"observed_annualised": round(obs, 3),
                               "expected_max_haircut": haircut,
                               "net": net,
                               "pass": net > 0}
    R["Q5"] = boot

    # Q6 follower reality
    fh = [dict(r) for r in conn.execute(
        "SELECT ts, followers, copiers FROM follower_history ORDER BY ts")]
    R["Q6"] = {
        "follower_points": len(fh),
        "first": fh[0] if fh else None, "last": fh[-1] if fh else None,
        "note": ("leader return over follower windows is Q1's; follower return = "
                 "leader's monthly PnL starting AFTER their join month (computed in "
                 "REPORT when join dates are visible). Copy slippage assumed 1 lot "
                 "of spread + 5bp unless measured.") if fh else "STAGE1 DATA MISSING"
    }

    # Q7 survivorship
    meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM leader_meta")}
    R["Q7"] = {
        "leaderboard_rows_in_export": sum(
            1 for r in conn.execute(
                "SELECT COUNT(*) FROM har_raw WHERE url LIKE '%leader%'")),
        "note": ("rank-one-year-ago requires a leaderboard export (or the leaders "
                 "list page) — compute rank of 6001's trailing-12m PnL among peers "
                 "at 2025-10-23.") if meta else "STAGE1 DATA MISSING",
    }

    print(json.dumps(R, indent=1, default=str))
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "interrogate_results.json"), "w") as fh:
        json.dump(R, fh, indent=1, default=str)


if __name__ == "__main__":
    sys.exit(main() or 0)
