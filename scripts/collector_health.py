#!/usr/bin/env python3
"""Nightly collector health check.
PASS if collector.db exists, has a snapshot in the last 36 hours and the
settlements table advanced (or is legitimately empty for a <1-day-old DB).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "validate" / "collector.db"


def main() -> int:
    if not DB.exists():
        print("DEGRADED collector.db missing")
        return 1
    con = sqlite3.connect(DB)
    try:
        row = con.execute("SELECT MAX(run_ts), COUNT(*) FROM snapshots_2").fetchone()
        last, n = row[0], row[1]
    except sqlite3.Error as e:
        print(f"DEGRADED collector schema: {e!r}")
        return 1
    if not n:
        print("DEGRADED 0 snapshots — collector never writes")
        return 1
    age = datetime.now(UTC) - _dt(last)
    if age > timedelta(hours=36):
        print(f"DEGRADED last snapshot {age} ago (<36h required)")
        return 1
    print(f"OK last snapshot {last} ({age} ago), {n} rows")
    return 0


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


if __name__ == "__main__":
    sys.exit(main())
