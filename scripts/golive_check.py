#!/usr/bin/env python3
"""golive_check.py — automated go-live gate checks (prints WHICH gate failed).

Exit code 0 = all gates that require a pass actually pass. LIVE mode is
impossible unless this script exits 0 AND the operator passes
--i-understand-this-is-live with explicit --env=live.

Gates (task spec):
  1 model-free option-leg results (collector.db >= 8 weeks real bids +
    settlements, >= 3 days with >4% underlying move, bootstrap lower bound
    of daily P&L net of spreads/fees/GST > 0, worst day within risk budget)
  2 walk-forward OOS positive & not failing deflated Sharpe (logged configs)
  3 >= 4 weeks paper with backtest parity, zero unresolved reconciliation
    mismatches
  4 >= 2 weeks testnet soak with fault injection passing
  5 sizing: P(ruin) < 1% at chosen size, user-approved max drawdown written
  6 kill switch, alerts and rollback tested in the last 7 days
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

TIER1 = [
    "gate1_model_free_option_legs",
    "gate2_walk_forward_oos",
    "gate3_paper_parity",
    "gate4_testnet_soak",
    "gate5_sizing_ruin",
    "gate6_ops_drills",
]
MAX_DAILY_RISK_USD = float(os.getenv("MAX_DAILY_RISK_USD", "50"))


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def gate1() -> tuple[bool, str]:
    """Model-free option-leg gate from validate/collector.db."""
    db = ROOT / "validate" / "collector.db"
    if not db.exists():
        return False, "collector.db missing (start validate/collector.py --loop)"
    con = sqlite3.connect(db)

    def _dt(ts: str | float) -> datetime:
        try:
            return datetime.fromtimestamp(float(ts), tz=UTC)
        except (ValueError, TypeError, OverflowError, OSError):
            return datetime(1970, 1, 1, tzinfo=UTC)

    a_row = con.execute("SELECT MIN(run_ts), MAX(run_ts) FROM snapshots_2").fetchone()
    a, b = a_row[0], a_row[1]
    if a is None or b is None:
        return False, "no collector snapshots: no model-free data"
    weeks = (_dt(b) - _dt(a)).days / 7
    if weeks < 8:
        return False, f"only {weeks:.1f} weeks of snapshots (need >= 8)"
    settle = con.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    if settle < 8 * 5:
        return False, f"{settle} settlements (need daily settles for 8 weeks)"
    rows = con.execute(
        "SELECT run_ts, underlying_price FROM snapshots_2 "
        "WHERE underlying_price IS NOT NULL ORDER BY run_ts"
    ).fetchall()
    move_days: set[str] = set()
    spot: dict[str, float] = {}
    for ts, sp in rows:
        d = _dt(ts).strftime("%Y-%m-%d")
        spot[d] = float(sp)  # last index print of the day
    days = sorted(spot)
    for p, q in zip(days, days[1:], strict=False):
        if abs(spot[q] / spot[p] - 1) > 0.04:
            move_days.add(q)
    if len(move_days) < 3:
        return False, f"only {len(move_days)} day-to-day >4% moves (need >= 3)"
    daily: dict[str, float] = {}
    for run_ts, pnl, units in con.execute(
        "SELECT run_ts, pnl_per_contract, COALESCE(units_sold,1) FROM settlements"
    ):
        d = _dt(run_ts).strftime("%Y-%m-%d")
        daily[d] = daily.get(d, 0.0) + float(pnl or 0) * max(units, 0)
    vals = sorted(daily.values())
    if len(vals) < 20:
        return False, f"only {len(vals)} settle-days, need >= 20"
    import random

    rng = random.Random(7)
    fifths = []
    for _ in range(2000):
        s = sorted(vals[rng.randrange(len(vals))] for _ in range(len(vals)))
        fifths.append(s[int(0.05 * len(s))] * len(s))  # P5 of total P&L
    p5 = sum(fifths) / len(fifths)
    if p5 <= 0:
        return False, f"bootstrap P5 daily-PnL {p5:.2f} <= 0"
    if min(vals) < -MAX_DAILY_RISK_USD:
        return False, f"worst day {min(vals):.2f} beyond budget {MAX_DAILY_RISK_USD}"
    return True, (
        f"{settle} settlements, {weeks:.1f}w, "
        f"{len(move_days)} >4% days, P5 {p5:.2f}, worst {min(vals):.2f}"
    )


def _fileGate(name: str, need: str) -> tuple[bool, str]:
    p = STATE / name
    if not p.exists():
        return False, f"{need}: no {p}"
    data = json.loads(p.read_text())
    return bool(data.get("passed")), str(data.get("detail", ""))


def gate2() -> tuple[bool, str]:
    ok, msg = _fileGate("wf_gate.json", "walk-forward OOS run")
    if not ok:
        return ok, f"walk-forward: {msg}"
    d = json.loads((STATE / "wf_gate.json").read_text())
    return bool(
        d.get("pnl") and d.get("pnl") > 0 and d.get("dsr_net", -1) > 0
    ), f"wf: pnl={d.get('pnl')} dsr={d.get('dsr_net')}"


def gate3() -> tuple[bool, str]:
    return _fileGate("paper_gate.json", "paper gate")


def gate4() -> tuple[bool, str]:
    return _fileGate("testnet_gate.json", "testnet soak")


def gate5() -> tuple[bool, str]:
    return _fileGate("sizing_gate.json", "sizing sign-off")


def gate6() -> tuple[bool, str]:
    p = STATE / "ops_drills.json"
    if not p.exists():
        return False, "no ops_drills.json"
    d = json.loads(p.read_text())
    tested_at = d.get("last_drill") or "1970-01-01"
    age = datetime.now(UTC) - datetime.fromisoformat(tested_at.replace("Z", "+00:00"))
    if age > timedelta(days=7):
        return False, f"ops drills {age.days}d ago (need < 7d)"
    return True, f"drills fresh ({tested_at})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    _ = ap.add_argument(
        "--i-understand-this-is-live",
        action="store_true",
        help="ignored here; live mode lives in main.py",
    )
    args = ap.parse_args()
    results = {}
    for i, fn in enumerate([gate1, gate2, gate3, gate4, gate5, gate6], start=1):
        try:
            ok, why = fn()
        except Exception as e:  # noqa: BLE001 — gates must not crash
            ok, why = False, f"gate error: {e!r}"
        results[TIER1[i - 1]] = {"passed": ok, "detail": why}
        mark = "PASS" if ok else "FAIL"
        print(f"{mark}  {TIER1[i - 1]}: {why}")
    if args.json:
        print("GATES_JSON_BEGIN")
        print(json.dumps(results, indent=1))
        print("GATES_JSON_END")
    all_ok = all(r["passed"] for r in results.values())
    failed = [k for k, r in results.items() if not r["passed"]]
    if not all_ok:
        print(f"GO-LIVE BLOCKED. Failing gates: {', '.join(failed)}")
        return 1
    print("ALL GATES PASS — live still requires the explicit operator flag.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
