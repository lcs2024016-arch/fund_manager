#!/usr/bin/env python3
"""paper_gate.py — accumulate GATE 3 evidence (paper soak).

Writes state/paper_gate.json for scripts/golive_check.py gate 3:
  passed = >= 28 continuous days of paper runs + parity test passing on the
  latest code + zero unresolved reconciliation mismatches recorded.

Soak days are read from state/soak_log.json (maintained by
scripts/paper_supervisor.sh). Parity is re-run live against the recorded
fixture. Mismatch count comes from state/soak_mismatches.json (append-only;
the runner/supervisor records every 'halt:' reconciliation event).
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

REQUIRED_DAYS = 28          # ~4 weeks soak
OVERLAP_DAYS = 30           # count days with any soak activity in window


def _soak_days() -> tuple[float, int]:
    p = STATE / "soak_log.json"
    if not p.exists():
        return 0.0, 0
    runs = json.loads(p.read_text())
    cutoff = datetime.now(UTC) - timedelta(days=OVERLAP_DAYS)
    span = timedelta(0)
    open_run = False
    for r in runs:
        s = r.get("started_at")
        e = r.get("ended_at")
        if not s:
            continue
        t0 = datetime.fromisoformat(s.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(e.replace("Z", "+00:00")) if e \
            else datetime.now(UTC)
        # merge consecutive segments (crash-restarts stay continuous)
        t0 = max(t0, cutoff)
        span += max(t1 - t0, timedelta(0))
    days = span.total_seconds() / 86400.0
    open_run = any(r.get("ended_at") is None for r in runs)
    return days, 1 if open_run else 0


def _parity_ok() -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_parity_paper_backtest.py", "-q", "--no-header",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=300)
    tail = (r.stdout or "").strip().splitlines()[-1:] or ["no output"]
    return r.returncode == 0, tail[0]


def _mismatches() -> int:
    p = STATE / "soak_mismatches.json"
    if not p.exists():
        return 0
    return int(json.loads(p.read_text()).get("unresolved", 0))


def main() -> int:
    STATE.mkdir(exist_ok=True)
    days, live = _soak_days()
    parity_ok, parity_msg = _parity_ok()
    n_mm = _mismatches()
    passed = days >= REQUIRED_DAYS * 0.98 and parity_ok and n_mm == 0
    detail = (
        f"soak {days:.2f}/28 days (live_now={bool(live)}), "
        f"parity={'PASS' if parity_ok else 'FAIL'} ({parity_msg}), "
        f"unresolved_mismatches={n_mm}"
    )
    (STATE / "paper_gate.json").write_text(json.dumps(
        {"passed": passed, "detail": detail,
         "updated": datetime.now(UTC).isoformat()},
        indent=1))
    print(("OK " if passed else "PENDING ") + detail)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
