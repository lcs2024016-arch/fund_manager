# Delta Exchange India "All-in-One" — realistic backtest & validation

## Files
* `delta_products.csv` (repo root) — all 2,138 Delta India products (symbol, type,
  underlying, tick, contract value, state, settlement time, maker/taker fees).
* `delta_discovery.py` — Task-1 script that wrote the CSV and the classifications.
* `aio_bt.py` — the v2 backtest / validation engine.
* `results_ablation.json`, `results_walkforward.json`, `events_v2.csv`, `equity_v2.csv` — outputs.
* `REPORT.md` — results & conclusions.

## Data reality (verified 2026-10-23)
* **Options are listed ONLY for BTC, ETH, XAUT.** PAXG, SLVON, OIL and all xStocks
  have perps but no options (the original script wrongly sold daily options on PAXG).
* **Expired option candles are NOT retrievable** from `/v2/history/candles`
  (every historical daily symbol returns 0 rows; only the newest expiry has data).
  → option premium = Black-Scholes(1d) with IV = trailing 20-day realised vol ×
    iv_mult, calibrated to the live chain on 2026-10-23 (1-day ATM BTC call
    IV ≈ 50%, ETH ≈ 80–96%; realised vol was 27% / 43%).  iv_mult(call)=2.0,
    iv_mult(put)=0.9, sold at bid with a 15% haircut.  Sensitivity rows prove the
    option P&L is largely this assumption.
* **No public funding-history endpoint** → constant 8h snapshot rates applied
  signed to marked notional.  Funding-off ablation shows it barely matters here.
* Candle history is short: ~5.5 months (2026-05-09 → 2026-10-23) at 1h.

## Fees modelled (Delta India tier 0, Oct-2026 published schedule)
Perps taker 5bp / maker 2bp; options 1bp of notional capped at 3.5% of premium;
settlement 1.5bp of notional capped at 5% of premium; 18% GST on all fees;
5bp taker slippage; limit fills must trade THROUGH a level; ambiguous bars
resolve to the worse outcome; cross margin 15%/10% with 2% liquidation penalty;
portfolio circuit breaker at 12% drawdown.

## Run
```bash
.venv/bin/python validate/aio_bt.py                 # ablation table, real data
.venv/bin/python validate/aio_bt.py --walkforward   # walk-forward validation
.venv/bin/python validate/aio_bt.py --synthetic     # random-walk control
.venv/bin/python validate/delta_discovery.py        # refresh delta_products.csv
```
Cache lives in `delta_cache/`; delete it to re-download.
