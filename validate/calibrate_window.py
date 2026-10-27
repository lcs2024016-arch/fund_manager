#!/usr/bin/env python3
"""
calibrate_window.py — Stage 4 calibration check against the leader's live window.

Runs longterm_bt's capped/FINAL replica engine over 2025-10-01..2026-10-22 at
$10k capital (engine's fixed size), rescales to the leader's $1,000 margin, and
compares monthly PnL with the leader's OBSERVED closed-trade months
(leader_trades.db). MODELLED vs OBSERVED is printed side by side.
"""
import json
import os
import sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

import longterm_bt as lbt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    dfs, fund_rows, dv = lbt.fetch_all()
    ts, rows = lbt.build_rows(dfs, dv)
    out = {}
    for name, d in (("+caps only (no breaker)", dict(caps=True)),
                    ("FINAL", dict(volscale=True, regime=True, decay=0.8,
                                   max_hold_days=3.0, caps=True, otm_k=0.8,
                                   optskip=True))):
        cfg = lbt.from_over(d)
        eq, ev, pa, liq = lbt.run_once(
            ts, rows, cfg,
            funding_rates={"BTCUSDT": lbt.funding_by_year(fund_rows["BTCUSDT"]),
                           "ETHUSDT": lbt.funding_by_year(fund_rows["ETHUSDT"])},
            tstart=pd.Timestamp("2025-10-01"), tend=pd.Timestamp("2026-10-23"))
        dly = lbt.daily(eq)
        m = dly.resample("MS").last().diff().dropna()
        out[name] = {str(k)[:7]: round(float(v) / 10, 2) for k, v in m.items()}

    con = sqlite3.connect(os.path.join(ROOT, "leader_trades.db"))
    obs = defaultdict(float)
    for ex, pnl in con.execute(
            "SELECT substr(exit_ts,1,7), SUM(realized_pnl) FROM trades "
            "WHERE source='csv' AND exit_ts >= '2025-10' GROUP BY 1"):
        obs[ex] = round(pnl, 2)
    print("month | OBSERVED(leader $1k) | +caps modelled | FINAL modelled")
    months = sorted(set(obs) | set(out["FINAL"]))
    for mo in months:
        print(f"{mo} | {obs.get(mo, 0):+8.2f} | "
              f"{out['+caps only (no breaker)'].get(mo, 0):+8.2f} | "
              f"{out['FINAL'].get(mo, 0):+8.2f}")
    json.dump({"observed": dict(obs), "modelled": out},
              open(os.path.join(HERE, "calibration_window.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
