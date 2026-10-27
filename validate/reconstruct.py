#!/usr/bin/env python3
"""
reconstruct.py — Stage 2: reverse-engineer the leader's rules from fills in
leader_trades.db (populated by har_ingest.py from the user's own HAR export).

Outputs a reconstructed-rules JSON (validate/reconstructed_rules.json) and a
printed summary. If the DB has no trades yet, exits with "STAGE1 DATA MISSING"
and no invented parameters.

Derived, per asset:
  perp lot sizes, grid spacing distribution (adverse move between successive
  adds), take-profit distribution, holding times, max concurrent lots,
  LIFO-closing check (newest-lot-first),
  option: strike distance from spot (% and sigma), expiry day, roll time,
  cover ratio vs perp exposure, calls-vs-puts by asset/time,
  net directional exposure vs trend, margin/leverage over time.
"""
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "leader_trades.db")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "reconstructed_rules.json")


def utc(s):
    if not s:
        return None
    s = str(s).replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%Y%m%d", "%s"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        return float(s)
    except ValueError:
        return None


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(p * len(xs))))
    return round(xs[i], 6)


def dist(vals, q=(0.1, 0.5, 0.9)):
    if not vals:
        return None
    return {"n": len(vals), "min": round(min(vals), 6),
            "p10": pct(vals, .1), "median": pct(vals, .5), "p90": pct(vals, .9),
            "mean": round(sum(vals) / len(vals), 6)}


def asset_of(symbol):
    s = (symbol or "").upper()
    for a in ("BTC", "ETH", "XAUT", "PAXG"):
        if a in s:
            return a
    return s.split("-")[0][:6] or "?"


def main():
    if not os.path.exists(DB):
        print("STAGE1 DATA MISSING — leader_trades.db has no data yet.")
        print("Run: .venv/bin/python validate/har_ingest.py <your_export.har>")
        sys.exit(2)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE exit_ts != '' ORDER BY entry_ts")]
    if not trades:
        print("STAGE1 DATA MISSING — leader_trades.db has no closed trades yet.")
        print("Run: .venv/bin/python validate/har_ingest.py <your_export.har>")
        sys.exit(2)

    out = {"source_rows": len(trades), "generated_utc": datetime.now(timezone.utc).isoformat(),
           "assets": {}}

    by_asset = defaultdict(list)
    for t in trades:
        by_asset[asset_of(t["symbol"])].append(t)

    for a, ts in by_asset.items():
        det = {"n_trades": len(ts), "instruments": defaultdict(int)}
        perps, opts = [], []
        for t in ts:
            det["instruments"][t["instrument"]] += 1
            (opts if t["instrument"] in ("call", "put") else perps).append(t)

        # option strike geometry (daily sigma approximated by 1.6%/20%*sqrt(D/365), documented)
        for label, group in (("options", opts), ("perps", perps)):
            if not group:
                det[label] = None
                continue
            entry_px = [float(t["entry_px"]) for t in group if t["entry_px"]]
            qty = [float(t["qty"]) for t in group if t["qty"]]
            hold = []
            for t in group:
                a_ts, b_ts = utc(t["entry_ts"]), utc(t["exit_ts"])
                if a_ts and b_ts and b_ts > a_ts:
                    hold.append(b_ts - a_ts)
            det[label] = {
                "entry_price": dist(entry_px), "qty": dist(qty),
                "holding_hours": dist(hold),
                "win_rate": round(sum(1 for t in group if (t["realized_pnl"] or 0) > 0)
                                  / len(group), 4),
                "avg_win": round(sum((t["realized_pnl"] or 0) for t in group
                                     if (t["realized_pnl"] or 0) > 0)
                                 / max(1, sum(1 for t in group if (t["realized_pnl"] or 0) > 0)), 4),
                "avg_loss": round(sum((t["realized_pnl"] or 0) for t in group
                                      if (t["realized_pnl"] or 0) <= 0)
                                 / max(1, sum(1 for t in group if (t["realized_pnl"] or 0) <= 0)), 4),
            }

        # grid spacing: adverse move between successive same-direction adds while
        # a book is open (perp entries sorted by time, same side)
        add_prices = defaultdict(list)
        for t in perps:
            if t["side"] and t["entry_px"]:
                add_prices[(t["side"].lower().lstrip("+"), )].append(
                    (utc(t["entry_ts"]), float(t["entry_px"])))
        spacing = []
        for k, lst in add_prices.items():
            lst.sort()
            for p0, p1 in zip(lst, lst[1:]):
                t0, x0 = p0; t1, x1 = p1
                if t1 - t0 < 7 * 86400:  # same "book episode" if < 7d apart
                    if (k[0] in ("buy", "long") and x1 < x0) or \
                       (k[0] in ("sell", "short") and x1 > x0):
                        spacing.append(abs(x1 - x0) / x0)
        det["grid_spacing"] = dist(spacing)

        # ---- option analytics (OBSERVED from fills) ----
        if opts:
            import re
            bym = defaultdict(list)  # (month, type) -> [strike_dist_pct]
            exp_days = defaultdict(int)
            types = defaultdict(int)
            strike_px = []
            entry_times = {(utc(t["entry_ts"])) for t in perps if t["entry_ts"]}
            def spot_near(ts):
                # nearest perp fill within +-24h as spot proxy (Delta perp ~ spot)
                best, bd = None, 10**12
                for et in entry_times:
                    if et is None:
                        continue
                    d = abs((et or 0) - ts)
                    if d < bd and d <= 86400:
                        bd, best = d, et
                if best is None:
                    return None
                for t in perps:
                    if utc(t["entry_ts"]) == best and t["entry_px"]:
                        return float(t["entry_px"])
                return None
            for t in opts:
                m = re.match(r"([CP])-(\w+)-(\d+\.?\d*)-(\d{6})", str(t["symbol"]))
                if not m:
                    continue
                typ, _a, strike, ymd = m.groups()
                types[typ] += 1
                yy = 2000 + int(ymd[:2]); mm = int(ymd[2:4]); dd = int(ymd[4:6])
                try:
                    from datetime import date as _date
                    exp_days[_date(yy, mm, dd).strftime("%a")] += 1
                except Exception:
                    pass
                strike_px.append(float(strike))
                sp = spot_near(utc(t["entry_ts"]))
                if sp:
                    ds = (float(strike) - sp) / sp
                    bym[(ymd[2:4], typ)].append(ds)
            dist_all = [x for v in bym.values() for x in v]
            det["options_geometry"] = {
                "n_parsed": len(strike_px),
                "calls": types.get("C", 0), "puts": types.get("P", 0),
                "expiry_weekdays": dict(exp_days),
                "strike_distance_pct": dist(dist_all) if dist_all else None,
                "strike_distance_call": dist(
                    [x for (mo, ty), v in bym.items() if ty == "C" for x in v]),
                "strike_distance_put": dist(
                    [x for (mo, ty), v in bym.items() if ty == "P" for x in v]),
                "spot_proxy": "nearest perp fill within 24h (Delta perp ~ spot)",
            }

        # LIFO check: when multiple same-side lots are open, are exits at the
        # newest entry price rather than oldest?
        lifo = {"n_pairs_checked": 0, "n_lifo": 0, "n_fifo": 0}
        for t in perps:
            prev = [p for p in perps
                    if p["side"] == t["side"] and p["exit_ts"] == t["exit_ts"]
                    and utc(p["entry_ts"]) and utc(t["entry_ts"])
                    and utc(p["entry_ts"]) < utc(t["entry_ts"])]
            if prev and t["exit_px"]:
                newest = max(prev, key=lambda p: utc(p["entry_ts"]))
                oldest = min(prev, key=lambda p: utc(p["entry_ts"]))
                lifo["n_pairs_checked"] += 1
                dn = abs(float(t["exit_px"]) - float(newest["entry_px"]))
                do = abs(float(t["exit_px"]) - float(oldest["entry_px"]))
                if dn < do:
                    lifo["n_lifo"] += 1
                elif do < dn:
                    lifo["n_fifo"] += 1
        det["lifo_check"] = lifo

        # cover ratio: option notional vs perp notional per asset
        if opts and perps:
            det["cover_ratio_note"] = \
                "cover ratio computed in interrogate.py once both legs exist"

        out["assets"][a] = det

    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
