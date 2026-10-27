#!/usr/bin/env python3
"""
import_csv.py — Stage 1 alternative ingest: manually/exported closed-trade CSV
into leader_trades.db (same schema har_ingest.py uses).

EXPECTED COLUMNS (leader_6001_closed_trades_*.csv, Mirrorpip export):
  Venue, Symbol, Side, Qty, Entry Price, Exit Price, SL, TP, PnL,
  Entry Time, Exit Time

Also (re)builds daily_equity from closed-trade realized PnL grouped by exit
day, since the HAR contained no equity series. This is a REALIZED-only equity
curve — unrealized marks are not in the export; that limitation is reported.

Optionally purges junk rows previously ingested from securities/list HAR
payloads (rows with no symbol / no timestamps / no PnL).

USAGE: .venv/bin/python validate/import_csv.py <csv> [--purge-junk]
"""
import csv
import glob
import hashlib
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "leader_trades.db")
HERE = os.path.dirname(os.path.abspath(__file__))


def connect():
    conn = sqlite3.connect(DB)
    conn.executescript(open(os.path.join(HERE, "_ddl.sql")).read()
                       if os.path.exists(os.path.join(HERE, "_ddl.sql"))
                       else har_ddl())
    return conn


def har_ddl():
    # identical DDL to har_ingest.py
    from validate_import_ddl import DDL  # not used; fallback below
    return ""


def purge_junk(conn):
    n = conn.execute("""DELETE FROM trades WHERE source='har' AND
        (symbol IS NULL OR symbol='None' OR instrument='perp' AND qty=0
         AND realized_pnl=0 AND entry_ts='')""").rowcount
    return n


def ingest(conn, path):
    rows = list(csv.DictReader(open(path, newline="")))
    base = os.path.basename(path)
    n_new = n_skip = 0
    for r in rows:
        try:
            sym = (r.get("Symbol") or "").strip()
            if not sym:
                n_skip += 1
                continue
            entry = (r.get("Entry Time") or "").strip()
            exit_ = (r.get("Exit Time") or "").strip()
            qty = float(r.get("Qty") or 0)
            epx = float(r.get("Entry Price") or 0)
            xpx = float(r.get("Exit Price") or 0)
            pnl = float(r.get("PnL") or 0)
            sl = float(r.get("SL") or 0)
            tp = float(r.get("TP") or 0)
            side = (r.get("Side") or "").strip()
            ext = "csv:" + hashlib.md5((base + json_row(r)).encode()).hexdigest()
            conn.execute(
                """INSERT INTO trades (ext_id, symbol, instrument, side, qty,
                     entry_ts, entry_px, exit_ts, exit_px, fees, realized_pnl,
                     leverage, margin, source, raw_json, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ext_id) DO NOTHING""",
                (ext, sym, infer_instrument(sym), side, qty,
                 entry, epx, exit_, xpx, None, pnl, None, None,
                 "csv", json_row(r), f"SL={sl} TP={tp}"))
            if conn.execute("SELECT changes()").fetchone()[0]:
                n_new += 1
            else:
                n_skip += 1
        except Exception as e:
            print("skip:", e, r)
    return n_new, n_skip, len(rows)


def json_row(r):
    import json
    return json.dumps(r, sort_keys=True)


def infer_instrument(symbol):
    s = (symbol or "").upper()
    if s.startswith("C-") or "-C-" in s:
        return "call"
    if s.startswith("P-") or "-P-" in s:
        return "put"
    return "perp"


def build_daily_equity(conn):
    """Realized-only daily PnL + cumulative realized equity by exit day."""
    day = defaultdict(float)
    for sym, exit_ts, pnl in conn.execute(
            "SELECT symbol, exit_ts, realized_pnl FROM trades "
            "WHERE source='csv' AND exit_ts != ''"):
        if exit_ts:
            day[str(exit_ts)[:10]] += pnl or 0
    conn.execute("DELETE FROM daily_equity")
    cum = 0.0
    for ts in sorted(day):
        cum += day[ts]
        conn.execute(
            "INSERT INTO daily_equity (ts, equity, pnl, realized) VALUES (?,?,?,?)",
            (ts, round(cum, 2), round(day[ts], 2), round(day[ts], 2)))
    return len(day), round(cum, 2)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    purge = "--purge-junk" in sys.argv
    if not args:
        print(__doc__)
        sys.exit(1)
    path = args[0]
    conn = sqlite3.connect(DB)
    ddl = """CREATE TABLE IF NOT EXISTS trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ext_id TEXT UNIQUE,
      symbol TEXT, instrument TEXT, side TEXT, qty REAL,
      entry_ts TEXT, entry_px REAL, exit_ts TEXT, exit_px REAL,
      fees REAL, realized_pnl REAL, leverage REAL, margin REAL,
      source TEXT, raw_json TEXT, notes TEXT);
    CREATE TABLE IF NOT EXISTS open_positions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, symbol TEXT,
      instrument TEXT, side TEXT, qty REAL, entry_px REAL, mark_px REAL,
      unrealized_pnl REAL, raw_json TEXT);
    CREATE TABLE IF NOT EXISTS daily_equity (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT UNIQUE, equity REAL,
      balance REAL, pnl REAL, realized REAL, unrealized REAL, raw_json TEXT);
    CREATE TABLE IF NOT EXISTS follower_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, followers INTEGER,
      copiers REAL, raw_json TEXT);
    CREATE TABLE IF NOT EXISTS leader_meta (
      key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS har_raw (
      id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, http_status INTEGER,
      fetched_ts TEXT, sha256 TEXT UNIQUE, record_json TEXT);"""
    conn.executescript(ddl)
    if purge:
        n = purge_junk(conn)
        print(f"purged {n} junk har rows")
    n_new, n_dup, total = ingest(conn, path)
    conn.commit()
    ndays, cum = build_daily_equity(conn)
    conn.commit()
    for t in ("trades", "daily_equity"):
        print(f"{t:15s} {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]} rows")
    print(f"csv rows {total}, new {n_new}, dup/skip {n_dup}; "
          f"daily_equity built {ndays} days, cumulative realized PnL ${cum}")
    conn.close()


if __name__ == "__main__":
    main()
