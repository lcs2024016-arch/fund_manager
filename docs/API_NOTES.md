# API NOTES — Delta Exchange India (repo client paths)

Canonical live-probing record: `validate/API_NOTES.md` (verified against
`api.india.delta.exchange` 2026-10-23). Summary of what this repo relies on:

## Endpoints used by shipped code
| Path | Authentic. | Notes |
|---|---|---|
| `GET /products` | public | full catalog; the recorded fixture
  `tests/fixtures/products_btcusd.json` is a real response for BTCUSD |
| `GET /products/{symbol}` | public | single product resolve |
| `GET /tickers` | public | comma-separated symbols; option tickers carry
  nested `quotes.{best_bid,best_ask,...}`, `greeks.*` |
| `GET /tickers/{symbol}` | public | **returns result as an object (dict)**
  for one symbol, a list for many — the paper port assumes the object shape |
| `GET /l2orderbook/{symbol}` | public | depth book (used for size-at-level checks) |
| `GET /history/candles` | public | OHLCV; `time` may be s or ms (normalized) |
| `GET /orders`, `POST /orders`, `DELETE /orders` | signed | spec 1.2/1.6
  envelope; status strings mapped in `core/oms.py::EX_STATUS` |
| WS `v2/channels` on `socket.india.delta.exchange` | public/signed | see
  `delta/ws.py`, heartbeat module |

## Auth (spec 1.2, existing `delta/auth.py`)
`signature = HMAC_SHA256(secret, METHOD + timestamp + path + query + raw_body)`
as hex, headers `api-key`, `timestamp`, `signature`; ~5 s validity window
(`SignatureExpired` handled by re-signing at most once). The intense caveat:
the docs' sample signature does not reproduce; tests recompute the HMAC
independently (`tests/test_auth.py`).

## Rate limits
`delta/ratelimit.py` implements the per-endpoint weights observed in the
spec; single-endpoint 429s are retried with backoff in the REST layer.

## Not verified / do not assume
- There is **no settlement-price endpoint**. Settlement PnL is approximated
  with the underlying index at expiry — documented in RISK.md.
- The `expiry_date` query filter on `GET /v2/tickers` accepted no format we
  tried; filter locally.
- Linear (perp/futures) fee rates remain UNVERIFIED
  (`core/fees.LinearDefaults`, TODO(VERIFY) before any live run).
- Testnet base: `https://cdn-ind.testnet.deltaex.org` (REST) with matching
  ws host; validated against the testnet base constant in `delta/rest.py`
  during the earlier harness work — re-verify when M4 starts.
