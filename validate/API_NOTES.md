# API NOTES — verified against live api.india.delta.exchange (2026-10-23)

## GET /v2/tickers (no key)
Verified working filters (comma-separated values allowed):
* `contract_types=call_options,put_options` → all option tickers (paged; BTC alone = 504 rows).
* `underlying_asset_symbols=BTC,ETH,XAUT` → works (plural form verified).
* `expiry_date` — **no date format was accepted in live probing** (tried `2026-10-24`,
  `27-10-26`, `271126`: 0 results, no error). Do NOT rely on it; filter locally by expiry.
* `symbol=C-BTC-79600-251026` → 1 ticker.

Option ticker fields confirmed present (real response):
```
symbol, contract_type, strike_price, underlying_asset_symbol, spot_price,
quotes: {best_bid, best_ask, bid_size, ask_size, bid_iv, ask_iv, mark_iv, mark_price,
         impact_mid_price (null in option books)},
greeks: {delta, gamma, theta, vega, rho, spot},
mark_price, open_interest via oi/oi_contracts/oi_value_usd, volume, turnover_usd,
timestamp (ns epoch), product_id, contract_value (0.001 BTC), tick_size,
price_band {lower_limit, upper_limit}, mark_change_24h, product_trading_status,
settlement_time (on the products object; tickers have it too via products parse)
```
Notes: `bid_iv`/`ask_iv` can be junk on stale books (e.g. 0.01 or 1.0), mark_iv is
Delta's own model IV. `spot_price` = the underlying index snapshot at quote time.

## GET /v2/l2orderbook/{symbol}
Confirmed active options return 10+ levels each side:
```
result: {symbol, buy: [{price,size,depth}...], sell: [...]}
```
Good for measuring size at/inside the bid. Calls are cheap; only snapshot
strikes within ±2.5% of spot (cap per underlying per run).

## GET /v2/products
`?contract_types=...&underlying_asset_symbols=...` works; fields used:
`symbol, contract_type, strike_price, settlement_time (ISO, always 12:00Z for dailies),
state, tick_size, contract_value`.  `maker_commission`/`taker_commission` = NULL for
all contracts → fees must come from the published fee schedule, not the API.
`state=expired` filter DOES return products (including some not-yet-expired ones —
do not trust `state=expired` alone; check `settlement_time < now`).

## Options listing scope (verified)
Option underlyings: **BTC, ETH, XAUT only.** Dailies every day 12:00 UTC (+ Fridays
to 16:00). Symbol format `C-BTC-79500-301026` (strike has no decimals for BTC/ETH;
XAUT strikes are integers).

## Settlement price (verified by probing)
* `GET /v2/settlement_prices` → 404. `GET /v2/settled_options` → 404.
* No settled-price field appears on expired products.
* Therefore the payoff must be derived from the **underlying index at expiry**:
  collector stores `spot_price` of the ticker every run (esp. the ~11:58 UTC
  snapshot) and uses it as the settlement price, with the last 11:58 option mark
  kept as a sanity bound. Delta's own settlement may be a short index TWAP —
  the approximation is documented in analyze_options.py output.

## Candle history
Expired option symbols return 0 rows from /v2/history/candles (verified on
multiple historical dailies). Only the most recent expiry candles exist.
Market data only — collector never sends auth and does not trade.

---

# Mirrorpip (trade.mirrorpip.com) — Stage-1 findings (2026-10-23)

## Service resolver (public, verified)
`GET https://trade.mirrorpip.com/lookup/v1/info` →
```
auth:          https://trade.mirrorpip.com/auth            (tag aa010)
user_lookup:   https://trade.mirrorpip.com/user_lookup     (tag ka010)
leader_auth:   https://leader.mirrorpip.com/ldapi/auth     (tag la010)
leader_lookup: https://leader.mirrorpip.com/leader_lookup  (tag jd010)
expiry_secs:   14400
```

## Auth state
All four leaf services returned `404 page not found` unauthenticated (paths probed:
`/v1/leaders`, `/leader/6001`, `/v1/leader/6001`, `/info`, headers Tag/X-Tag).
`main.dart.js` (7.25 MB Flutter build, greppable) confirms services
`feeds, feeds_api, securities, user_data, trades, trades_live, strategies,
leaders, followers` and the 401→"Session expired. Please login again." flow.
**Leader 6001's trades/equity/heatmap/followers are auth-gated.**

## Extraction protocol (chosen, allowed)
HAR export from the user's own logged-in browser session:
1. F12 → Network-tab, preserve log; login; open the leader dashboard;
   page through all closed trades, equity, heatmap, follower panels.
2. "Save all as HAR with content" → feed to `har_ingest.py` (offline, idempotent,
   strips Authorization/Cookie/token headers, keeps only JSON bodies matching
   trade/leader/pnl/equity/heatmap/position/follower paths).
3. `reconstruct.py` then `interrogate.py` produce numbers; `leader_trades.db`
   (root) tables: trades, open_positions, daily_equity, follower_history,
   leader_meta, har_raw.
