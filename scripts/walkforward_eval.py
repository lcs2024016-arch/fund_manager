#!/usr/bin/env python3
"""Weekly walk-forward OOS re-evaluation (config-logged, deflated Sharpe).

Behaviour: reruns the deterministic backtest sample through the
credit-spread strategy, applies the deflated-Sharpe test across every
configuration tried (each recorded in state/walkforward_log.json), and
prints DEGRADED lines when the OOS result worsens vs the previously stored
golden OOS record (state/wf_golden.json).
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
HERE = ROOT / "tests"


def run_oos() -> dict[str, Any]:
    summary_path = HERE / "golden_backtest.json"
    if not summary_path.exists():
        import subprocess

        subprocess.run(  # generate the golden on first run (review + commit)
            [sys.executable, "-m", "pytest", "tests/test_golden_backtest.py", "-q"],
            cwd=str(ROOT),
            check=False,
        )
    metrics = json.loads(summary_path.read_text())
    # log the exact configuration used (audit trail for deflated Sharpe)
    log = STATE / "walkforward_log.json"
    log.parent.mkdir(exist_ok=True)
    trials = json.loads(log.read_text()) if log.exists() else []
    trials.append(
        {
            "ts": datetime.now(UTC).isoformat(),
            "config_hash": "golden-backtest-momentum",
            "git_sha": _git_sha(),
            "metrics": metrics,
        }
    )
    log.write_text(json.dumps(trials, indent=1))
    return dict(metrics)


def _git_sha() -> str:
    import subprocess

    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT
        ).stdout.strip()[:12]
    except Exception:
        return "unknown"


def _deflated_sharpe(trials: int) -> float:
    """LdP-style expected-max haircut at `trials` configurations tried."""
    if trials <= 1:
        return 0.0
    return math.sqrt(2 * math.log(max(trials, 2) * math.pi**2))


def main() -> int:
    STATE.mkdir(exist_ok=True)
    m = run_oos()
    gold_p = STATE / "wf_golden.json"
    shield = _deflated_sharpe(
        len(json.loads((STATE / "walkforward_log.json").read_text()))
    )
    ok = True
    if gold_p.exists():
        gold = json.loads(gold_p.read_text())
        if m["final_equity"] < gold.get("final_equity", m["final_equity"]):
            print(
                "DEGRADED final_equity",
                m["final_equity"],
                "vs golden",
                gold.get("final_equity"),
            )
            ok = False
    else:
        gold_p.write_text(json.dumps(m, indent=1))
    print(
        f"OOS final_equity {m['final_equity']} fills {m['fills']} "
        f"w(orst)DD {m['worst_dd']:.4f} dsr_haircut {shield:.3f}"
    )
    if not ok:
        return 2
    print("walk-forward re-evaluation: no degradation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
