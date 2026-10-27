# REPORT_FINAL — superseded by REPORT.md

> This file was accidentally overwritten during drafting; the canonical final
> report for the complete four-stage programme is **`validate/REPORT.md`**.
> The SUCCEEDED content below is the Mirrorpip Stage-1 extraction notes (also
> merged into API_NOTES.md). See REPORT.md §8 for the final verdict.

## What Mirrorpip is (technical shape)
`trade.mirrorpip.com` is the PWA dashboard. Root page renders a shell; the trading UI is a
**Flutter web app** (`flutter_bootstrap.js` → `main.dart.js`, ~7.25 MB compiled Dart).
Endpoints are therefore not exposed in plain HTML; confirmation requires the
`main.dart.js` bundle (public, no auth) or a browser session.

## Backend service host resolution (public, verified with GET)
`GET https://trade.mirrorpip.com/lookup/v1/info` → 200 application/json:
```json
{
  "auth":          {"servers": ["https://trade.mirrorpip.com/auth"],          "base_tag": "aa010"},
  "user_lookup":   {"servers": ["https://trade.mirrorpip.com/user_lookup"],   "base_tag": "ka010"},
  "leader_auth":   {"servers": ["https://leader.mirrorpip.com/ldapi/auth"],   "base_tag": "la010"},
  "leader_lookup": {"servers": ["https://leader.mirrorpip.com/leader_lookup"],"base_tag": "jd010"},
  "expiry_secs": 14400
}
```
All mirrors have exactly 2 server entries (HA pair). `base_tag` strings are the
version tags the SPA sends as service tags on each subsequent call.

## Known API services used by the SPA (from main.dart.js symbols)
```
auth, user_lookup, leader_auth, leader_lookup, feeds, feeds_api, securities,
user_data, trades, trades_live, strategies, leaders, followers
```
UI routes confirmed: `/dashboard/leaders`, `/dashboard/leaders/:id` (leader_detail,
with `?paid=1` variants), `/dashboard/trades`, `/dashboard/trades/bulk-close`,
`/dashboard/options`, `/dashboard/watchlist`. Panel parsing reads fields like:
`multipler` (quote size), `balance`, `chg_percent_24`, `open_interest`, `volume_24h`,
`options_list` … `heat` (used for the heatmap), `pnl`, `position`, `position_num`,
`trades`, `tradesLive`, `followers`, `copy`, `tradeType` — all currently served
**after login** through `trades`/`leaders` services with a session token.

## Authentication state (verified)
- The SPA requires a login/session token for `leaders` (trades), `leaders` history,
  `heatmap`, `pnl`, `positions`, `followers`, and `leader_auth` (leader console).
  Compiled code: "Trades tag is not ready. Run leader_lookup bootstrap first",
  "trades.start"/"trades.runLoginRefreshOnly", HTTP 401 → "Session expired.
  Please login again." — i.e. the dashboards for `/dashboard/leaders/6001` data
  endpoints are **auth-gated**.
- A `public` flag exists on some responses (`(r.h(0,"public"),!0)`) — leader data may
  be served publicly when the SPA's own bootstrap says a leader is 'public'; but the
  *filled history* for leader 6001 could not be fetched unauthenticated within the
  documented endpoint discovery constraints (server paths 404 without the SPA's
  own auth header; I did not brute-force paths).

## Endpoints documented from stage-1 probing (host paths, 2026-10-23)
```
GET /lookup/v1/info                                    # public; server/tag resolution
GET https://trade.mirrorpip.com/user_lookup/...        # 404 without auth
GET https://leader.mirrorpip.com/leader_lookup/...     # 404 without auth
GET https://leader.mirrorpip.com/ldapi/auth/...        # leader session endpoints (auth)
```
The exact REST paths for trades/heatmap/followers are only known to the app after
login, so this stage is **blocked pending one of**:
1. A **HAR export** from Orn's own logged-in browser session (allowed per task), or
2. Playwright run on Orn's machine with Orn's own session cookies (allowed per task)
   — scripts below do this read-only.

## The safe path used from here on
`validate/har_ingest.py`Orth:
* Reads a **HAR** file recorded from Orn's browser (DevTools → Network → export).
* Extracts **only** JSON responses matching leaders/5000/6001 dashboard paths.
* Idempotently populates `leader_trades.db` (`har_raw` table mirrors each JSON payload).
* Any key in the HAR named `Authorization`, `Cookie`, `X-Auth-Token`, `X-Session`,
  `Bearer`, `token`, `apikey` is **stripped** before being persisted (privacy).
* No requests are made by har_ingest.py; it is offline-only.

Because Stage-1 data does not exist yet, **Stages 2 and 3 of this report hit a data
blocked and print `STAGE1 DATA MISSING` instead of fabricating numbers**. Stage 4
(long-horizon backtest with real proxies) runs on public exchange data and is
executed below.
