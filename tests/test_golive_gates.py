"""Gate framework tests: golive_check prints WHICH gate fails; gates are
data-file driven so they are auditable. Also: paper-mode enforced by default."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_gates_report_individually():
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "golive_check.py"), "--json"],
        capture_output=True,
        text=True,
    )
    s = r.stdout
    after = s.split("GATES_JSON_BEGIN", 1)[1].split("GATES_JSON_END", 1)[0]
    data = json.loads(after)
    names = [
        "gate1_model_free_option_legs",
        "gate2_walk_forward_oos",
        "gate3_paper_parity",
        "gate4_testnet_soak",
        "gate5_sizing_ruin",
        "gate6_ops_drills",
    ]
    for n in names:
        assert n in data, n
        assert isinstance(data[n]["passed"], bool)
    assert r.returncode == 1  # nothing has genuinely passed yet
    # every FAIL must carry a reason string
    for n, v in data.items():
        if not v["passed"]:
            assert v["detail"], n


def test_paper_mode_is_the_default():
    """Neither default nor CI may enable live trading."""
    main_py = (ROOT / "src" / "main.py").read_text()
    assert "--i-understand-this-is-live" in main_py
    cfg = (ROOT / "config.yaml").read_text()
    assert "mode: paper" in cfg
    # the credit-spread config must not carry a live flag at all
    cs = (ROOT / "config" / "credit_spread.yaml").read_text()
    assert "mode: live" not in cs and "allow_live" not in cs
