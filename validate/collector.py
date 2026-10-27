#!/usr/bin/env python3
"""
Delta Exchange India daily option-chain collector (read-only, no API key).

Snapshots (UTC) the BTC/ETH/XAUT option chain for the nearest daily expiry
(+ next expiry) and stores everything in SQLite (WAL). Idempotent inserts;
every snapshot row keeps the raw JSON so nothing is lost.

Schedule (UTC): 12:05  16:00  20:00  00:00  04:00  08:00  and 11:58 (pre-expiry).
Run as a cron job or loop — see README.md. Every run is recorded in `runs`.

Tables:
  snapshots(snapshot_id UNIQUE(text key per option per run), run_ts, spot,
            and full quote fields + raw_json)
  settlements(expiry_date, underlying, symbol, strike, kind, settle_index_price,
              entry_bid, payoff, pnl_per_contract, units_sold)
  runs(run_ts, kind, status, detail, n_rows, duration_ms)

Also records l2orderbook depth at the bid for strikes within +/-2.5% of spot
(needed to judge whether selling at the bid fills at real size).
"""
import argparse, json, os, sqlite3, sys, time
from datetime import datetime, timedelta, timezone
from math import isnan

import requests

BASE = "https://api.india.delta.exchange"
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "collector.db")
UNDERLYINGS = ("BTC", "ETH", "XAUT")
SCHEDULE = [(12, 5), (16, 0), (20, 0), (0, 0), (4, 0), (8, 0), (11, 58)]  # UTC
STRIKE_WINDOW = 0.10         # +/-10% of spot
L2_WINDOW = 0.025            # depth captured within +/-2.5%
OPTION_FEE_NOTIONAL, OPTION_FEE_CAP = 0.0001, 0.035
GST = 0.18
MAX_PAGES = 6


def now_utc(): return datetime.now(timezone.utc)


def db_conn():
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    _init_schema(con)
    try:
        con.execute("ALTER TABLE settlements ADD COLUMN cv REAL DEFAULT 0.001")
    except sqlite3.OperationalError:
        pass
    return con


def _init_schema(con):
    con.executescript("""
CREATE TABLE IF NOT EXISTS snapshots(
  snapshot_id TEXT PRIMARY KEY,
  run_ts TEXT NOT NULL,
  kind TEXT, settlement_time TEXT, expiry_ts TEXT,
  symbol TEXT, underlying TEXT, kind_opt TEXT, strike REAL,
  best_bid REAL, best_ask REAL, bid_size REAL, ask_size REAL,
  mark_price REAL, mark_iv REAL, bid_iv REAL, ask_iv REAL,
  delta REAL, gamma REAL, theta REAL, vega REAL, open_interest REAL, volume REAL,
  spot REAL, raw_json TEXT);
CREATE INDEX IF NOT EXISTS idx_snap_run ON snapshots(run_ts);
CREATE INDEX IF NOT EXISTS idx_snap_sym ON snapshots(symbol);
CREATE TABLE IF NOT EXISTS snapshots_2(
  snapshot_id TEXT PRIMARY KEY,
  run_ts TEXT NOT NULL, kind TEXT, symbol TEXT, underlying TEXT,
  call_put TEXT, strike REAL, expiry_ts TEXT, settlement_time TEXT,
  best_bid REAL, best_ask REAL, bid_size REAL, ask_size REAL,
  mark_price REAL, mark_iv REAL, bid_iv REAL, ask_iv REAL,
  delta REAL, gamma REAL, theta REAL, vega REAL,
  open_interest REAL, volume REAL, underlying_price REAL,
  l2_bid_depth REAL, contract_value REAL, raw TEXT);
CREATE INDEX IF NOT EXISTS idx_s2_run  ON snapshots_2(run_ts);
CREATE INDEX IF NOT EXISTS idx_s2_sym  ON snapshots_2(symbol);
CREATE TABLE IF NOT EXISTS settlements(
  expiry_date TEXT, symbol TEXT, underlying TEXT, call_put TEXT, strike REAL,
  settle_index_price REAL, entry_bid REAL, payoff REAL, pnl_per_contract REAL,
  units_sold REAL, run_ts TEXT, cv REAL DEFAULT 0.001,
  PRIMARY KEY(expiry_date, symbol, run_ts));
CREATE TABLE IF NOT EXISTS runs(
  run_ts TEXT, kind TEXT, status TEXT, detail TEXT, n_rows INTEGER, duration_ms INTEGER);
CREATE TABLE IF NOT EXISTS alerts(
  ts TEXT, message TEXT);
""")


def log_run(con, kind, status, detail="", n=0, dur=0):
    con.execute("INSERT INTO runs(run_ts,kind,status,detail,n_rows,duration_ms) VALUES(?,?,?,?,?,?)",
                (now_utc().isoformat(), kind, status, detail[:500], n, dur))
    con.commit()


def alert(con, msg):
    con.execute("INSERT INTO alerts(ts,message) VALUES(?,?)", (now_utc().isoformat(), msg))
    con.commit()
    print(f"[ALERT] {msg}", file=sys.stderr)


def fetch(url, params=None, tries=4):
    last = None
    for i in range(tries):
        try:
            r = requests.get(f"{BASE}{url}", params=params or {}, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(1.5 * (i + 1)); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e; time.sleep(1.5 * (i + 1))
    raise last


def f(x):
    try:
        v = float(x)
        return None if isnan(v) else v
    except (TypeError, ValueError):
        return None


def target_expiry(products, now=None):
    """nearest daily expiry set at/after now, and the next one."""
    now = now or now_utc()
    exps = sorted({p.get("settlement_time") for p in products
                   if p.get("settlement_time") and datetime.fromisoformat(
                       p["settlement_time"].replace("Z", "+00:00")) >= now.replace(second=0)})
    return exps[:2]


def snapshot(con, kind):
    t0 = time.time()
    run_ts = now_utc().isoformat(timespec="seconds")
    n = 0
    try:
        for ua in UNDERLYINGS:
            # perps ticker for underlying index price (xStocks use perp spot as index)
            idx = None
            try:
                tj = fetch("/v2/tickers", dict(contract_types="perpetual_futures",
                                               underlying_asset_symbols=ua, per_page=50))
                perp = [x for x in tj.get("result") or [] if x.get("symbol") == f"{ua}USD"]
                if perp:
                    idx = f(perp[0].get("spot_price")) or f(perp[0].get("mark_price"))
            except Exception as e:
                alert(con, f"{ua} index fetch failed: {e}")
            # product list for this underlying
            prods, page = [], 1
            while page <= MAX_PAGES:
                j = fetch("/v2/products", dict(contract_types="call_options,put_options",
                                               underlying_asset_symbols=ua,
                                               per_page=200, page=page))
                part = j.get("result") or []
                prods += part
                if len(part) < 200: break
                page += 1
            keep = [p for p in prods if p["symbol"].startswith(("C-", "P-"))
                    and "-" + ua + "-" in p["symbol"]]
            if not keep:
                alert(con, f"{ua}: no option products found"); continue
            expir = target_expiry(keep)
            if not expir:
                alert(con, f"{ua}: no upcoming expiry?"); continue
            wanted = {p["symbol"]: p for p in keep
                      if p.get("settlement_time") in expir}
            if idx is not None:
                wanted = {s: p for s, p in wanted.items()
                          if abs(f(p["strike_price"]) / idx - 1) <= STRIKE_WINDOW}
            tj = fetch("/v2/tickers", dict(contract_types="call_options,put_options",
                                           underlying_asset_symbols=ua, per_page=200, page=1))
            tix = {t.get("symbol"): t for t in (tj.get("result") or [])}
            for page in range(2, MAX_PAGES + 1):
                tj2 = fetch("/v2/tickers", dict(contract_types="call_options,put_options",
                                                underlying_asset_symbols=ua,
                                                per_page=200, page=page))
                part = tj2.get("result") or []
                tix.update({t.get("symbol"): t for t in part})
                if len(part) < 200: break
            for sym, p in wanted.items():
                t = tix.get(sym)
                q = (t or {}).get("quotes") or {}
                g = (t or {}).get("greeks") or {}
                st = p.get("settlement_time")
                row = dict(
                    snapshot_id=f"{run_ts}|{sym}",
                    run_ts=run_ts, kind=kind, symbol=sym, underlying=ua,
                    call_put=("call" if sym.startswith("C") else "put"),
                    strike=f(p.get("strike_price")), expiry_ts=st, settlement_time=st,
                    best_bid=f(q.get("best_bid")), best_ask=f(q.get("best_ask")),
                    bid_size=f(q.get("bid_size")), ask_size=f(q.get("ask_size")),
                    mark_price=f((t or {}).get("mark_price")),
                    mark_iv=f(q.get("mark_iv")), bid_iv=f(q.get("bid_iv")), ask_iv=f(q.get("ask_iv")),
                    delta=f(g.get("delta")), gamma=f(g.get("gamma")), theta=f(g.get("theta")),
                    vega=f(g.get("vega")),
                    open_interest=f((t or {}).get("oi")), volume=f((t or {}).get("volume")),
                    underlying_price=idx,
                    l2_bid_depth=None,
                    contract_value=f((t or {}).get("contract_value")) or 0.001,
                    raw=json.dumps(t) if t else json.dumps({"missing_ticker": True, "product": p}))
                cols = ", ".join(row.keys())
                ph = ", ".join("?" * len(row))
                con.execute(f"INSERT OR REPLACE INTO snapshots_2({cols}) VALUES({ph})",
                            list(row.values()))
                n += 1
            # l2 depth for near strikes (One call per underlying per run, capped)
            near = [s for s, p in wanted.items()
                    if idx and f(p["strike_price"]) and abs(f(p["strike_price"]) / idx - 1) <= L2_WINDOW]
            for sym in near[:12]:
                try:
                    ob = fetch(f"/v2/l2orderbook/{sym}")
                    buys = (ob.get("result") or {}).get("buy") or []
                    depth0 = f(buys[0]["size"]) if buys else None
                    con.execute("UPDATE snapshots_2 SET l2_bid_depth=? WHERE snapshot_id=?",
                                (depth0, f"{run_ts}|{sym}"))
                except Exception as e:
                    alert(con, f"l2 {sym}: {e}")
        con.commit()
        log_run(con, "snapshot", "ok", f"kind={kind}", n, int((time.time() - t0) * 1000))
        return n
    except Exception as e:
        log_run(con, "snapshot", "error", repr(e), n, int((time.time() - t0) * 1000))
        alert(con, f"snapshot run failed: {e}")
        return n


def parse_when(s):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%d%m%y"):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d
        except Exception:
            pass
    try:
        return datetime.strptime(s, "%d%m%y").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def settle_expired(con):
    """For every run_ts 12:05 'entry' snapshot whose options have now expired,
    record settlement = underlying index price at expiry (from the pre-expiry
    run's underlying_price / the nearest snapshot) and payoff per sold contract."""
    t0 = time.time(); n = 0
    try:
        rows = con.execute("""
            SELECT run_ts, underlying, call_put, strike, symbol, best_bid, expiry_ts,
                   contract_value, underlying_price
            FROM snapshots_2 WHERE kind IN ('entry','preexpiry') AND best_bid IS NOT NULL""").fetchall()
        done = {(r[0], r[4]) for r in con.execute(
            "SELECT DISTINCT run_ts, symbol FROM settlements").fetchall()}
        # settlement index price: underlying_price from the last pre-expiry/entry snapshot
        for (run_ts, ua, side, strike, sym, bid, exp, cv, spot_at_entry) in rows:
            key = (run_ts, sym)
            if key in done:
                continue
            exp_dt = parse_when(exp)
            if exp_dt is None or exp_dt > now_utc():
                continue
            # choose the snapshot closest to but not before expiry: simplest = max run_ts <= expiry+5min
            idx_row = con.execute("""
               SELECT underlying_price, run_ts FROM snapshots_2
               WHERE underlying=? AND run_ts<=? AND underlying_price IS NOT NULL
               ORDER BY run_ts DESC LIMIT 1""", (ua, exp.isoformat())).fetchone()
            if not idx_row:
                continue
            settle_idx = idx_row[0]
            entry_bid = bid
            cv = cv or 0.001
            payoff = max(settle_idx - strike, 0) if side == "call" else max(strike - settle_idx, 0)
            # option fee per contract sold: min(1bp*notional, 3.5%*premium), +GST
            notional = settle_idx * cv
            fee = min(OPTION_FEE_NOTIONAL * notional, OPTION_FEE_CAP * entry_bid * cv) * (1 + GST)
            pnl = entry_bid * cv - payoff * cv - fee
            con.execute("""INSERT OR REPLACE INTO settlements
                (expiry_date,symbol,underlying,call_put,strike,settle_index_price,
                 entry_bid,payoff,pnl_per_contract,units_sold,run_ts,cv)
                VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                        (exp[:10], sym, ua, side, strike, settle_idx, entry_bid,
                         payoff * cv, pnl, now_utc().isoformat(), cv))
            n += 1
        con.commit()
        log_run(con, "settlement", "ok", f"entries seen={len(rows)}", n, int((time.time() - t0) * 1000))
        return n
    except Exception as e:
        log_run(con, "settlement", "error", repr(e), n, int((time.time() - t0) * 1000))
        alert(con, f"settlement run failed: {e}")
        return n


def due_now():
    t = now_utc()
    for (h, m) in SCHEDULE:
        if t.hour == h and abs(t.minute - m) <= 2:
            return "entry" if (h, m) == (12, 5) else ("preexpiry" if (h, m) == (11, 58) else "watch")
    # allow hourly catch-up runs from cron every 15 min
    if t.minute % 15 == 0:
        return None
    return None


def watchdog(con):
    """Alert if the last 12:05 entry snapshot is stale (missed schedule)."""
    t = now_utc()
    if t.hour < 12 or (t.hour == 12 and t.minute < 5):
        return
    row = con.execute("SELECT MAX(run_ts) FROM snapshots_2 WHERE kind='entry'").fetchone()
    if not row or not row[0]:
        alert(con, "no entry snapshot recorded yet after 12:05 UTC")
        return
    try:
        last = datetime.fromisoformat(row[0])
    except ValueError:
        return
    if (t - last) > timedelta(days=1, minutes=30):
        alert(con, f"stale entry snapshot: last {row[0]}")


def once(kind):
    kind = kind or "adhoc"
    con = db_conn()
    try:
        n = snapshot(con, kind)
        s = settle_expired(con)
        print(f"[{now_utc().isoformat()}] kind={kind} snapshots={n} settlements={s}")
    finally:
        con.close()


def loop():
    con = db_conn()
    try:
        while True:
            kind = due_now()
            if kind:
                snapshot(con, kind)
                if kind in ("entry", "preexpiry"):
                    settle_expired(con)
            else:
                watchdog(con)
            time.sleep(60)
    finally:
        con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", nargs="?", const="adhoc", default=None)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    if args.db != DB:
        globals()["DB"] = args.db
    if args.loop:
        loop()
    else:
        once(args.once)
