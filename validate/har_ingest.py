#!/usr/bin/env python3
"""
har_ingest.py — offline ingest of a user-exported HAR into leader_trades.db.

USAGE
  .venv/bin/python validate/har_ingest.py /path/to/export.har

RULES (per task constraints)
  * Offline only: makes zero network requests.
  * Works on the user's OWN session export (HAR from browser DevTools).
  * Never persists credentials: any request header named like Authorization,
    Cookie, Set-Cookie, token, apikey, session is stripped before storage.
  * Idempotent: re-ingesting the same HAR does not duplicate rows.

DB SCHEMA (leader_trades.db, at repo root)
  trades          one row per closed/flat trade fill (option or perp)
  open_positions  snapshot of current open positions
  daily_equity    equity / balance / pnl series points
  follower_history  leader follower-count history
  leader_meta     leader profile metadata (key - value JSON)
  har_raw         every extracted JSON response, dedup by hash, with URL+ts
"""
import hashlib
import json
import os
import sqlite3
import sys

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "leader_trades.db")

SENSITIVE = ("authorization", "cookie", "set-cookie", "x-auth-token",
             "x-session", "x-csrf", "token", "apikey", "api-key", "bearer",
             "password", "secret")

PATH_HINTS = ("trade", "leader", "pnl", "equity", "heatmap", "follower",
              "position", "history", "closed", "open", "overview")

DDL = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ext_id TEXT UNIQUE,             -- platform trade id if present
  symbol TEXT, instrument TEXT,   -- instrument: perp | call | put | unknown
  side TEXT, qty REAL,
  entry_ts TEXT, entry_px REAL,
  exit_ts TEXT, exit_px REAL,
  fees REAL, realized_pnl REAL,
  leverage REAL, margin REAL,
  source TEXT,                    -- har
  raw_json TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS open_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  captured_at TEXT, symbol TEXT, instrument TEXT, side TEXT, qty REAL,
  entry_px REAL, mark_px REAL, unrealized_pnl REAL, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS daily_equity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT UNIQUE, equity REAL, balance REAL, pnl REAL,
  realized REAL, unrealized REAL, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS follower_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, followers INTEGER, copiers REAL, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS leader_meta (
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS har_raw (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT, http_status INTEGER, fetched_ts TEXT,
  sha256 TEXT UNIQUE, record_json TEXT
);
"""


def scrub(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k.lower() in SENSITIVE else scrub(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


def infer_instrument(symbol):
    s = (symbol or "").upper()
    if "C-" in s or s.startswith("C"):
        if "-C-" in s:
            return "call"
    if "-P-" in s:
        return "put"
    return "perp"


def find_kv(d, keys, default=None):
    """Case-insensitive recursive lookup for the first key present."""
    lw = {k.lower() for k in keys}

    def rec(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k.lower() in lw and not isinstance(v, (dict, list)):
                    return v
            for v in o.values():
                r = rec(v)
                if r is not None:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = rec(v)
                if r is not None:
                    return r
        return None
    r = rec(d)
    return r if r is not None else default


def ingest_trades(conn, url, body):
    rows = []
    if isinstance(body, dict):
        for k in ("trades", "closed_trades", "trade_history", "history", "data"):
            if isinstance(body.get(k), list):
                rows = body[k]
                break
    elif isinstance(body, list):
        rows = body
    for t in rows:
        if not isinstance(t, dict):
            continue
        sym = find_kv(t, ("symbol", "instrument_name", "product_symbol"))
        et_in = find_kv(t, ("entry_time", "opened_at", "created_at", "timestamp"))
        et_x = find_kv(t, ("exit_time", "closed_at", "close_time"))
        conn.execute(
            """INSERT INTO trades (ext_id, symbol, instrument, side, qty,
                 entry_ts, entry_px, exit_ts, exit_px, fees, realized_pnl,
                 leverage, margin, source, raw_json, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ext_id) DO NOTHING""",
            (str(find_kv(t, ("id", "trade_id", "uid")) or hashlib.md5(
                json.dumps(t, sort_keys=True).encode()).hexdigest()),
             str(sym), infer_instrument(str(sym)),
             str(find_kv(t, ("side", "direction", "type")) or ""),
             float(find_kv(t, ("qty", "size", "quantity", "lots"), 0) or 0),
             str(et_in or ""), float(find_kv(t, ("entry_price", "avg_entry",
                                                 "open_price"), 0) or 0),
             str(et_x or ""), float(find_kv(t, ("exit_price", "avg_exit",
                                                "close_price"), 0) or 0),
             float(find_kv(t, ("fees", "commission", "fee"), 0) or 0),
             float(find_kv(t, ("realized_pnl", "pnl", "profit", "net_pnl"), 0) or 0),
             float(find_kv(t, ("leverage",), 0) or 0),
             float(find_kv(t, ("margin",), 0) or 0),
             "har", json.dumps(scrub(t)), url[:200]))


def ingest_equity(conn, url, body):
    series = body
    if isinstance(body, dict):
        for k in ("equity", "series", "points", "daily_pnl", "curve", "data"):
            if isinstance(body.get(k), list):
                series = body[k]
                break
    if not isinstance(series, list):
        return
    for p in series:
        if not isinstance(p, (dict, list)):
            continue
        if isinstance(p, list):
            ts, eq = (str(p[0]), float(p[1]) if len(p) > 1 else None)
            row = dict(ts=ts, equity=eq)
        else:
            row = p
        conn.execute(
            """INSERT INTO daily_equity (ts, equity, balance, pnl, realized,
                 unrealized, raw_json)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(ts) DO NOTHING""",
            (str(find_kv(row, ("ts", "time", "date", "timestamp"), "") or ""),
             find_kv(row, ("equity", "value", "nav")),
             find_kv(row, ("balance", "wallet")),
             find_kv(row, ("pnl", "daily_pnl", "profit")),
             find_kv(row, ("realized", "realized_pnl")),
             find_kv(row, ("unrealized", "unrealized_pnl")),
             json.dumps(scrub(row))))


def ingest_followers(conn, url, body):
    if isinstance(body, dict) and isinstance(body.get("followers"), (int, float)):
        pass
    series = body
    for k in ("followers", "history", "copy_count", "series"):
        if isinstance(body, dict) and isinstance(body.get(k), list):
            series = body[k]
            break
    if not isinstance(series, list):
        return
    for p in series:
        if not isinstance(p, (dict, list)):
            continue
        conn.execute(
            """INSERT INTO follower_history (ts, followers, copiers, raw_json)
               VALUES (?,?,?,?)""",
            (str(find_kv(p, ("ts", "time", "date", "timestamp"), "") or ""),
             find_kv(p, ("followers", "count", "value")),
             find_kv(p, ("copiers", "aum", "amount")),
             json.dumps(scrub(p))))


def ingest_meta(conn, body, now):
    if not isinstance(body, dict):
        return
    for k, v in body.items():
        try:
            j = json.dumps(scrub(v))
        except Exception:
            continue
        conn.execute(
            """INSERT INTO leader_meta (key, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                 updated_at=excluded.updated_at""", (k, j, now))


def main(har_path):
    with open(har_path, "r", errors="replace") as fh:
        har = json.load(fh)
    entries = har.get("log", {}).get("entries", [])
    conn = sqlite3.connect(DB)
    conn.executescript(DDL)
    n_seen = n_new = 0
    import datetime
    now = datetime.datetime.utcnow().isoformat() + "Z"
    for e in entries:
        req = e.get("request", {})
        resp = e.get("response", {})
        url = req.get("url", "")
        body = resp.get("content", {}).get("text", "")
        if not url or not body:
            continue
        if not any(h.lower() in url.lower() for h in PATH_HINTS):
            continue
        try:
            payload = json.loads(body)
        except Exception:
            continue  # non-JSON (html/css/js) skipped
        payload = scrub(payload)
        raw = json.dumps(payload, sort_keys=True)
        sha = hashlib.sha256(raw.encode()).hexdigest()
        cur = conn.execute(
            "INSERT OR IGNORE INTO har_raw (url, http_status, fetched_ts, sha256,"
            " record_json) VALUES (?,?,?,?,?)",
            (url, resp.get("status"), e.get("startedDateTime"), sha, raw[:200000]))
        n_seen += 1
        n_new += cur.rowcount
        if cur.rowcount:
            ingest_trades(conn, url, payload)
            ingest_equity(conn, url, payload)
            ingest_followers(conn, url, payload)
            ingest_meta(conn, payload, now)
    conn.commit()
    for t in ("trades", "daily_equity", "follower_history", "leader_meta", "har_raw"):
        c = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:18s} {c} rows")
    conn.close()
    print(f"har entries scanned {n_seen}, new stored {n_new}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
