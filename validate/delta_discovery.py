#!/usr/bin/env python3
"""Task 1: pull /v2/products, build delta_products.csv, classify, check symbol coverage
and whether expired option candles are retrievable."""
import json, os, sys, time
import requests

BASE = "https://api.india.delta.exchange"
OUT = os.path.join(os.path.dirname(__file__), "..", "delta_products.csv")
DISC = os.path.join(os.path.dirname(__file__), "..", "delta_products.json")
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

r = requests.get(f"{BASE}/v2/products", params={"per_page": 500}, timeout=60)
r.raise_for_status()
r2 = requests.get(f"{BASE}/v2/products", params={"per_page": 500, "page": 2}, timeout=60)
prods = r.json()["result"] + (r2.json().get("result") or [])
json.dump(prods, open(DISC, "w"))
print(f"{len(prods)} products")

import csv
import re
from collections import defaultdict

with open(OUT, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["symbol","contract_type","underlying_asset","quoting_asset","description","tick_size",
                "contract_value","contract_value_currency","state","settlement_time","maker_fee","taker_fee"])
    for p in prods:
        mf = p.get("maker_commission"); tf = p.get("taker_commission")
        mf = f"{float(mf):g}" if mf is not None else ""
        tf = f"{float(tf):g}" if tf is not None else ""
        w.writerow([p.get("symbol"), p.get("contract_type"), p.get("underlying_asset"),
                    p.get("quoting_asset"), (p.get("description") or "")[:120],
                    p.get("tick_size"), p.get("contract_value"), p.get("contract_value_currency"),
                    p.get("state"), p.get("settlement_time"), mf, tf])

by_ct = defaultdict(int)
for p in prods:
    by_ct[p.get("contract_type")] += 1
print("\nCounts by contract_type:", dict(by_ct))

def group(p):
    sym = p.get("symbol") or ""
    ct = p.get("contract_type") or ""
    ua = (p.get("underlying_asset") or {}).get("symbol", "")
    if ct == "perpetual_futures":
        if sym.startswith(("PAXG", "XAUT")): return "perp_rwa_gold"
        if sym.startswith(("SLV", "OIL")): return "perp_rwa_other"
        if re.search(r"X$|XUSD$", sym): return "perp_xstock"
        if sym.endswith(("USD", "USDT", "USDC")) and ua in ("BTC","ETH","SOL","XRP","DOGE","AVAX","ADA","BNB","LINK","TRX","MATIC","SUI","PEPPER"): return "perp_crypto"
        return "perp_other"
    if ct in ("call_options","put_options","move_options"):
        ol = (p.get("options") or {}).get("underlying") if isinstance(p.get("options"), dict) else None
        return f"opt_{ua}_{'call' if ct=='call_options' else 'put' if ct=='put_options' else 'move'}"
    if ct == "futures":
        return "fut_options_underlying" if re.match(r"C-|P-|MOVE", sym) is None else "other"
    if ct == "spot": return "spot"
    return ct

g = defaultdict(int)
gsyms = defaultdict(list)
for p in prods:
    k = group(p); g[k] += 1; gsyms[k].append(p.get("symbol"))
print("\nCounts by group:", dict(g))

wanted = ["ETHUSD","BTCUSD","PAXGUSD","XAUTUSD","SLVONUSD","OILUSD","COINXUSD","QQQXUSD",
          "SPYXUSD","AAPLXUSD","AMZNXUSD","TSLAXUSD","NVDAXUSD"]
syms = {p["symbol"]: p for p in prods}
print("\n--- symbol coverage ---")
for s in wanted:
    p = syms.get(s)
    print(f"{s:>10}: {'OK state=' + str(p.get('state')) if p else 'MISSING'}"
          + (f" ct={p.get('contract_type')} ua={(p.get('underlying_asset') or {}).get('symbol')}" if p else ""))
baba = sorted([s for s in syms if "BABA" in s])
print("BABA* contracts:", baba)

# which underlyings have listed options
opt_underlyings = defaultdict(lambda: [0,0])
for p in prods:
    ct = p.get("contract_type")
    if ct in ("call_options","put_options"):
        ua = (p.get("underlying_asset") or {}).get("symbol")
        ol = p.get("options") or {}
        ex = (ol.get("expiry_time") or "").split("T")
        opt_underlyings[ua][0 if ct=="call_options" else 1] += 1
print("\n--- option underlyings (calls, puts) ---")
for ua,(c,pp) in sorted(opt_underlyings.items()):
    print(f"{ua:>10}: calls={c:>4} puts={pp:>4}")

# derivatives of option underlyings: do perps exist for same underlying?
perp_uas = {(p.get("underlying_asset") or {}).get("symbol") for p in prods if p.get("contract_type")=="perpetual_futures"}
print("\nOption underlyings that also have a perp:", sorted(set(opt_underlyings) & perp_uas))
