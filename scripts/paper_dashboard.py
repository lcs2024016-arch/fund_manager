#!/usr/bin/env python3
"""paper_dashboard.py — live web dashboard for the paper-trading journals.

Serves on 127.0.0.1 (first free port from --port downward) and shows, per
strategy (A: rwa_positional, B: all_in_one):

  * equity curve + daily-PnL bars (from paper/<strategy>/daily.csv)
  * current holdings table: size / entry / mark / notional / unrealized
    (from paper/<strategy>/paper_state.json, contract value from trades)
  * recent fills (paper/<strategy>/trades.csv)
  * headline stats: equity, cash, unrealized, day PnL, PnL vs start

No third-party dependencies; charts are drawn with plain canvas.
Refresh: the page re-fetches /api/summary every 15 s.

Run:  python scripts/paper_dashboard.py [--port 8591]
"""
from __future__ import annotations

import argparse
import csv
import json
import socket
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPER = ROOT / "paper"
LABEL = {"rwa_positional": "A · rwa_positional", "all_in_one": "B · all_in_one"}


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_strategy(name: str) -> dict:
    d = PAPER / name
    out: dict = {"name": name, "label": LABEL.get(name, name)}

    meta_path = d / "meta.json"
    if meta_path.exists():
        out["meta"] = json.loads(meta_path.read_text())
    else:
        out["meta"] = {}

    # contract value per symbol comes from the trade journal
    cv: dict[str, float] = {}
    trades: list[dict] = []
    trades_path = d / "trades.csv"
    if trades_path.exists():
        # entry basis per symbol -> realized PnL on exit fills
        entry: dict[str, dict] = {}
        with trades_path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                sym = row.get("symbol", "")
                if sym:
                    cv[sym] = _f(row.get("contract_value"), cv.get(sym, 1.0))
                price = _f(row.get("price"))
                size = int(row.get("size") or 0)
                kind = "exit" if row.get("reduce_only") == "1" else "open"
                realized = None
                if kind == "exit" and sym:
                    e = entry.get(sym)
                    if e:
                        # close economics: pnl = (exit - entry) * position * cv,
                        # position = -exit_size (exit size is the opposite sign)
                        realized = (price - e["price"]) * (-size) * cv.get(sym, 1.0)
                elif sym and kind == "open":
                    entry[sym] = {"price": price, "size": size}
                row["kind"] = kind
                row["realized"] = realized
                trades.append(row)
    out["cv"] = cv
    out["trades"] = trades

    daily: list[dict] = []
    daily_path = d / "daily.csv"
    if daily_path.exists():
        with daily_path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                row["equity"] = _f(row.get("equity"))
                row["cash"] = _f(row.get("cash"))
                row["unrealized"] = _f(row.get("unrealized"))
                ops = row.get("open_positions", "")
                try:
                    row["open_positions"] = json.loads(ops) if ops else {}
                except json.JSONDecodeError:
                    row["open_positions"] = {}
                daily.append(row)
    out["daily"] = daily

    # minute-wise equity (append-only 1 row/min from the running session)
    intraday: list[dict] = []
    intraday_path = d / "intraday.csv"
    if intraday_path.exists():
        with intraday_path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                row["equity"] = _f(row.get("equity"))
                row["unrealized"] = _f(row.get("unrealized"))
                intraday.append(row)
    # keep the payload bounded: charts need at most the last ~8h (1440 rows)
    out["intraday"] = intraday[-1440:]

    # current account state (marks refreshed by the running session loop)
    state: dict = {}
    state_path = d / "paper_state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            state = {}

    cash = _f(state.get("equity"))
    holdings = []
    for sym, p in (state.get("positions") or {}).items():
        size = int(p.get("size") or 0)
        if size == 0:
            continue
        entry, mark = _f(p.get("entry")), _f(p.get("mark"))
        cval = cv.get(sym, 1.0)
        unreal = size * (mark - entry) * cval
        holdings.append({
            "symbol": sym,
            "size": size,
            "entry": entry,
            "mark": mark,
            "contract_value": cval,
            "notional": abs(size) * mark * cval,
            "unrealized": unreal,
        })
    unreal_total = sum(h["unrealized"] for h in holdings)

    initial = _f(out["meta"].get("initial_equity"))
    equity = cash + unreal_total
    stats = {
        "equity": equity,
        "cash": cash,
        "unrealized": unreal_total,
        "pnl_total": equity - initial if initial else None,
        "pnl_total_pct": ((equity - initial) / initial * 100.0) if initial else None,
    }
    if len(daily) >= 2:  # change vs the previous session-close snapshot
        stats["day_pnl"] = daily[-1]["equity"] - daily[-2]["equity"]
    elif len(daily) == 1:
        stats["day_pnl"] = daily[-1]["equity"] - initial
    else:
        stats["day_pnl"] = None
    out["stats"] = stats
    out["holdings"] = holdings
    out["days_indexed"] = len(daily)
    return out


def build_summary() -> dict:
    strategies = [load_strategy(d.name) for d in sorted(PAPER.iterdir())
                  if d.is_dir() and not d.name.startswith(".")]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategies": strategies,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if ctype == "application/json":
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/summary"):
            body = json.dumps(build_summary()).encode()
            self._send(200, body, "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/favicon.ico":
            self._send(204, b"", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Fund Manager · Paper PnL</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0e1116;--card:#161b22;--line:#242c38;--txt:#dbe2ea;--dim:#8b97a5;
--green:#3fb96f;--red:#e5484d;--gold:#e2b34b;
--tag-exit-bg:#2a1f10;--tag-exit-fg:#e2b34b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}
header{padding:14px 22px;border-bottom:1px solid var(--line);display:flex;
align-items:baseline;gap:14px;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:600}
header .upd{color:var(--dim);font-size:12px}
main{padding:18px 22px;max-width:1100px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:18px}
.card h2{margin:0 0 10px;font-size:15px;font-weight:600}
.stats{display:flex;gap:26px;flex-wrap:wrap;margin:6px 0 14px}
.stat b{display:block;font-size:17px;font-weight:600}
.stat span{color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.06em}
.up{color:var(--green)} .dn{color:var(--red)} .flat{color:var(--dim)}
canvas{width:100%;height:190px;display:block}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:780px){.grid{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px}
th{color:var(--dim);text-align:left;font-weight:500;font-size:11px;
text-transform:uppercase;letter-spacing:.05em;padding:6px 10px;
border-bottom:1px solid var(--line)}
td{padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:last-child td{border-bottom:none}
td.num,th.num{font-variant-numeric:tabular-nums;text-align:right}
.tag-exit{color:var(--tag-exit-fg);background:var(--tag-exit-bg);
 border:1px solid var(--line);border-radius:3px;padding:1px 5px;font-size:10px}
.note{color:var(--dim);font-size:12px;margin-top:10px}
h3.sub{font-size:13px;color:var(--dim);margin:16px 0 4px;font-weight:600;
text-transform:uppercase;letter-spacing:.05em}
</style>
</head>
<body>
<header><h1>📊 Fund Manager — Paper Portfolio</h1>
<span class="upd" id="upd">loading…</span></header>
<main id="app">loading…</main>
<script>
const fmt=(x,d=2)=>x==null?"—":(x<0?"-$"+Math.abs(x).toLocaleString(undefined,
 {minimumFractionDigits:d,maximumFractionDigits:d})
 :"$"+x.toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d}));
const pct=x=>x==null?"—":(x>=0?"+":"")+x.toFixed(2)+"%";
const cls=x=>x==null?"flat":(x>=0?"up":"dn");
const sgn=(x)=>(x>=0?"+":"-");

function lineChart(cv,labels,values){
 const c=cv.getContext("2d"),dpr=devicePixelRatio||1;
 const W=cv.clientWidth,H=cv.clientHeight;
 cv.width=W*dpr;cv.height=H*dpr;c.setTransform(dpr,0,0,dpr,0,0);
 c.clearRect(0,0,W,H);
 if(!values.length)return;
 const min=Math.min(...values),max=Math.max(...values);
 const pad=(max-min)*0.1||1,lo=min-pad,hi=max+pad;
 const L=54,R=W-12,T=12,B=H-18;
 const X=i=>values.length>1?L+(R-L)*i/(values.length-1):(L+R)/2;
 const Y=v=>B-(B-T)*(v-lo)/(hi-lo);
 c.strokeStyle="#242c38";c.fillStyle="#8b97a5";c.font="10px sans-serif";
 for(let k=0;k<=3;k++){const v=lo+(hi-lo)*k/3,y=Y(v);
  c.beginPath();c.moveTo(L,y);c.lineTo(R,y);c.stroke();
  c.fillText(v.toFixed(0),4,y+3);}
 const grad=c.createLinearGradient(0,T,0,B);
 grad.addColorStop(0,"rgba(63,185,111,.30)");grad.addColorStop(1,"rgba(63,185,111,0)");
 c.beginPath();values.forEach((v,i)=>i?c.lineTo(X(i),Y(v)):c.moveTo(X(i),Y(v)));
 c.lineTo(X(values.length-1),B);c.lineTo(X(0),B);c.closePath();
 c.fillStyle=grad;c.fill();
 c.beginPath();values.forEach((v,i)=>i?c.lineTo(X(i),Y(v)):c.moveTo(X(i),Y(v)));
 c.strokeStyle="#3fb96f";c.lineWidth=2;c.stroke();
 const last=values[values.length-1];
 c.beginPath();c.arc(X(values.length-1),Y(last),3,0,7);
 c.fillStyle="#3fb96f";c.fill();
 c.save();c.translate(T?12:0,H-4);c.fillStyle="#8b97a5";c.restore();
}

function barChart(cv,labels,values){
 const c=cv.getContext("2d"),dpr=devicePixelRatio||1;
 const W=cv.clientWidth,H=cv.clientHeight;
 cv.width=W*dpr;cv.height=H*dpr;c.setTransform(dpr,0,0,dpr,0,0);
 c.clearRect(0,0,W,H);
 if(!values.length)return;
 const max=Math.max(...values.map(Math.abs))||1;
 const L=54,R=W-12,T=12,B=H-16,mid=(T+B)/2;
 const bw=Math.max(4,(R-L)/values.length*0.55);
 values.forEach((v,i)=>{
  const x=L+(R-L)*Math.max(0,(i+0.5)/values.length);
  const y=mid-(B-T)*(v/(2*max));
  c.fillStyle=v>=0?"#3fb96f":"#e5484d";
  c.fillRect(x-bw/2,Math.min(y,mid),bw,Math.max(2,Math.abs(y-mid)));});
 c.strokeStyle="#242c38";c.beginPath();c.moveTo(L,mid);c.lineTo(R,mid);c.stroke();
 c.fillStyle="#8b97a5";c.font="10px sans-serif";
 c.fillText("+"+max.toFixed(0),4,T+8);
 c.fillText("-"+max.toFixed(0),4,B-2);
}

function holdingsTable(h){
 if(!h.length)return "<div class='note'>No open positions.</div>";
 const rows=h.map(p=>`<tr><td>${p.symbol}</td>
  <td class="num ${p.size<0?"dn":"up"}">${sgn(p.size)}${Math.abs(p.size)}</td>
  <td class="num">${p.entry.toFixed(2)}</td>
  <td class="num">${p.mark.toFixed(2)}</td>
  <td class="num">${fmt(p.notional)}</td>
  <td class="num ${cls(p.unrealized)}">${fmt(p.unrealized,true)}</td></tr>`).join("");
 return `<table><tr><th>Symbol</th><th class="num">Size</th>
  <th class="num">Entry</th><th class="num">Mark</th>
  <th class="num">Notional</th><th class="num">Unrealized PnL</th></tr>${rows}</table>
  <div class="note">size &lt; 0 = short · notional = |size| × mark × contract value</div>`;
}

function tradesTable(t){
 if(!t.length)return "<div class='note'>No fills yet.</div>";
 const rows=t.slice(-12).reverse().map(t=>{
   const exit=t.kind==="exit";
   return `<tr>
   <td>${t.ts.split("T")[1].slice(0,8)} UTC</td>
   <td>${t.symbol}</td><td>${t.side}</td>
   <td>${exit?'<b class="tag-exit">EXIT</b>':'open'}</td>
   <td class="num">${t.size}</td>
   <td class="num">${(+t.price).toFixed(2)}</td>
   <td class="num">${fmt(+(t.fee||0))}</td>
   <td class="num ${cls(t.realized)}">${fmt(t.realized,true)}</td></tr>`}).join("");
 return `<table><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Type</th>
 <th class="num">Size</th><th class="num">Price</th><th class="num">Fee</th>
 <th class="num">Realized PnL</th></tr>${rows}</table>`;
}

function strategyCard(s){
 const st=s.stats,base=+(s.meta.initial_equity||0);
 return `<div class="card"><h2>${s.label}</h2>
 <div class="stats">
  <div class="stat"><span>Equity</span><b>${fmt(st.equity)}</b></div>
  <div class="stat"><span>Cash</span><b>${fmt(st.cash)}</b></div>
  <div class="stat"><span>Unrealized</span>
    <b class="${cls(st.unrealized)}">${fmt(st.unrealized,true)}</b></div>
  <div class="stat"><span>Latest day PnL</span>
    <b class="${cls(st.day_pnl)}">${fmt(st.day_pnl,true)}</b></div>
  <div class="stat"><span>Total PnL</span>
    <b class="${cls(st.pnl_total)}">${fmt(st.pnl_total,true)}
      <span class="${cls(st.pnl_total)}" style="font-size:12px">
      (${pct(st.pnl_total_pct)})</span></b></div>
  <div class="stat"><span>Days</span><b>${s.daily.length}</b></div>
 </div>
 <div class="grid">
  <div><h3 class="sub">Equity curve</h3><canvas id="eq_${s.name}"></canvas></div>
  <div><h3 class="sub">Daily PnL</h3><canvas id="dp_${s.name}"></canvas></div>
  <div><h3 class="sub">Minute-wise PnL (latest 8h, 1m bars)</h3><canvas id="im_${s.name}"></canvas></div>
 </div>
 <h3 class="sub">Holdings</h3>${holdingsTable(s.holdings)}
 <h3 class="sub">Recent fills</h3>${tradesTable(s.trades)}
 </div>`;
}

function render(d){
 document.getElementById("upd").textContent =
   "updated " + new Date(d.generated_at).toLocaleTimeString();
 const totalEq=d.strategies.reduce((a,s)=>a+(s.stats.equity||0),0);
 const totalPnl=d.strategies.reduce((a,s)=>a+(s.stats.pnl_total||0),0);
 document.getElementById("app").innerHTML =
  `<div class="card"><div class="stats">
   <div class="stat"><span>Total equity</span><b>${fmt(totalEq)}</b></div>
   <div class="stat"><span>Total PnL</span>
     <b class="${cls(totalPnl)}">${fmt(totalPnl,true)}</b></div>
  </div></div>`
  + d.strategies.map(strategyCard).join("")
  + "<div class='note'>Paper money — simulated fills, real market data. "
  + "Source: paper/&lt;strategy&gt;/ (daily.csv · paper_state.json · trades.csv). "
  + "Marks refresh every 15 s from the running session.</div>";
 // equity line + daily pnl bars; day 1 PnL = equity - initial_equity
 d.strategies.forEach(s=>{
   lineChart(document.getElementById("eq_"+s.name),
             s.daily.map(r=>r.date),s.daily.map(r=>r.equity));
   const base=+(s.meta.initial_equity||0);
   const bars=s.daily.map((r,i)=>i?r.equity-s.daily[i-1].equity:r.equity-base);
   barChart(document.getElementById("dp_"+s.name),s.daily.map(r=>r.date),bars);
   // minute-wise PnL: per-minute equity delta; first point vs day open
   const iv=s.intraday||[];
   if(iv.length>=2){
     const day=iv[0].ts.slice(0,10);
     const pts=iv.filter(r=>r.ts.slice(0,10)===day);
     if(pts.length>=2){
       const labels=pts.map(r=>r.ts.split("T")[1].slice(0,5));
       const vals=pts.map((r,i)=>i?r.equity-pts[i-1].equity:r.equity-pts[0].equity);
       barChart(document.getElementById("im_"+s.name),labels,vals);
     }
   }
 });
}

async function refresh(){
 try{
  const r=await fetch("/api/summary");
  render(await r.json());
 }catch(e){
  document.getElementById("upd").textContent="api unreachable: "+e;
 }
}
refresh();
setInterval(refresh,15000);
</script>
</body>
</html>"""


def find_free_port(start: int) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"no free port found from {start}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8591)
    args = ap.parse_args()
    port = find_free_port(args.port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"paper dashboard → http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
