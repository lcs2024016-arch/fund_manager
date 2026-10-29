# Paper-trading report (B+C+A)

_Paper money for each strategy: simulated fills on real Delta market data; zero orders sent, no API keys used. Generated 2026-10-29 06:34 UTC._

| Strategy | Equity | P&L | P&L % | Days (G/R) | Max DD | Fees |
|---|---|---|---|---|---|---|
| `all_in_one` (B) | $99,779.68 | -$220.32 | -0.22% | 8 (2/1) | -0.27% | $82.17 |
| `credit_spread` (C) | $100,000.00 | +$0.00 | +0.00% | 5 (0/0) | +0.00% | $0.00 |
| `rwa_positional` (A) | $105,883.85 | +$5,883.85 | +5.88% | 8 (4/4) | -3.68% | $87.44 |

> Strategies trade *separate simulated accounts*; their numbers are independent (per-strategy equity, PnL, fees).

---
## Strategy B — `all_in_one`

_Initial equity $100,000.00. Start 2026-10-22._

Equity **$99,779.68** · P&L **-$220.32** (-0.22%) · trading days **8** (green 2 / red 1) · max drawdown **-0.27%** · fees $82.17

- Closed lots: 0
- Open unrealized PnL: **-$167.81**
- Open positions: `BTCUSD -62`, `ETHUSD +171`, `PAXGUSD -986`, `XAUTUSD -490`
_(Option P&L, when applicable, is an approximate Black-Scholes model.)_

### PnL calendar
### October 2026  — month -$220.32

| Mon | Tue | Wed | Thu | Fri | Sat | Sun |
|---|---|---|---|---|---|---|
|   |   |   | · | · |   |   |
| · | · | · | · | · |   |   |
| · | · | · | · | · |   |   |
| · | · | · | **22** 0.00 | **23** 0.00 | **24** 0.00 | **25** 0.00 |
| **26** 0.00 | **27** -$273.41 | **28** +$9.10 | **29** +$43.98 | · |   |   |

_(UTC days. `**day** +$PnL` = recorded session day; `·` = weekday with
no session; weekends without data left blank. Future days left blank.)_

### Daily stats (last 30 recorded days)

| Date | Day PnL | Equity (MTM) | Cash | Unrealized | Trades | Fees | Sessions | Open positions |
|---|---|---|---|---|---|---|---|---|
| 2026-10-29 | +$43.98 | $99,779.68 | $99,947.49 | -$167.81 | 0 | $0.00 | 1 | BTCUSD-62, ETHUSD+171, PAXGUSD-986, XAUTUSD-490 |
| 2026-10-28 | +$9.10 | $99,735.70 | $99,947.49 | -$211.79 | 0 | $0.00 | 3 | BTCUSD-62, ETHUSD+171, PAXGUSD-986, XAUTUSD-490 |
| 2026-10-27 | -$273.41 | $99,726.59 | $99,947.49 | -$220.89 | 66 | $82.17 | 9 | BTCUSD-62, ETHUSD+171, PAXGUSD-986, XAUTUSD-490 |
| 2026-10-26 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 3 | — |
| 2026-10-25 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 4 | — |
| 2026-10-24 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 4 | — |
| 2026-10-23 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 4 | — |
| 2026-10-22 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 2 | — |

_(Day PnL: closing equity vs previous recorded day close; MTM includes
 unrealized PnL of open positions. Trades = fills journaled that day.)_

### Trades (last 25 fills, newest first)

| Time (UTC) | Symbol | Side | Size | Price | Notional | Fee | Kind |
|---|---|---|---|---|---|---|---|
| 2026-10-27 14:47:22 | XAUTUSD | SELL | 490 | 3677.92 | $1,802.18 | $0.18 | `lotentry` |
| 2026-10-27 14:47:18 | PAXGUSD | SELL | 986 | 4052 | $3,995.27 | $0.40 | `lotentry` |
| 2026-10-27 14:47:15 | ETHUSD | BUY | 171 | 2328.45 | $3,981.65 | $1.99 | `lotentry` |
| 2026-10-27 14:47:14 | BTCUSD | SELL | 62 | 80227 | $4,974.07 | $2.49 | `lotentry` |
| 2026-10-27 07:30:07 | XAUTUSD | BUY | 488 | 4550 | $2,220.40 | $0.22 | `lotentry` |
| 2026-10-27 07:30:04 | PAXGUSD | SELL | 995 | 4014.75 | $3,994.68 | $0.40 | `lotentry` |
| 2026-10-27 07:30:00 | BTCUSD | SELL | 62 | 79699.5 | $4,941.37 | $2.47 | `lotentry` |
| 2026-10-27 07:23:51 | BTCUSD | BUY | 62 | 79673 | $4,939.73 | $2.47 | `deriskexit` |
| 2026-10-27 07:22:27 | PAXGUSD | BUY | 995 | 4018 | $3,997.91 | $0.40 | `deriskexit` |
| 2026-10-27 07:22:27 | XAUTUSD | SELL | 489 | 4087.16 | $1,998.62 | $0.20 | `deriskexit` |
| 2026-10-27 07:21:31 | XAUTUSD | BUY | 489 | 4360.47 | $2,132.27 | $0.21 | `lotentry` |
| 2026-10-27 07:21:27 | PAXGUSD | SELL | 995 | 4015.24 | $3,995.16 | $0.40 | `lotentry` |
| 2026-10-27 07:21:25 | ETHUSD | BUY | 173 | 2305.85 | $3,989.12 | $1.99 | `lotentry` |
| 2026-10-27 07:21:24 | BTCUSD | SELL | 62 | 79672 | $4,939.66 | $2.47 | `lotentry` |
| 2026-10-27 07:19:38 | ETHUSD | SELL | 173 | 2304.25 | $3,986.35 | $1.99 | `deriskexit` |
| 2026-10-27 07:18:13 | BTCUSD | BUY | 62 | 79668.5 | $4,939.45 | $2.47 | `deriskexit` |
| 2026-10-27 07:16:47 | PAXGUSD | BUY | 996 | 4015.09 | $3,999.03 | $0.40 | `deriskexit` |
| 2026-10-27 07:16:47 | XAUTUSD | SELL | 489 | 3763.35 | $1,840.28 | $0.18 | `deriskexit` |
| 2026-10-27 07:15:49 | XAUTUSD | BUY | 489 | 4085 | $1,997.57 | $0.20 | `lotentry` |
| 2026-10-27 07:15:44 | PAXGUSD | SELL | 996 | 4013.63 | $3,997.58 | $0.40 | `lotentry` |
| 2026-10-27 07:15:42 | ETHUSD | BUY | 173 | 2301.4 | $3,981.42 | $1.99 | `lotentry` |
| 2026-10-27 07:15:41 | BTCUSD | SELL | 62 | 79580.5 | $4,933.99 | $2.47 | `lotentry` |
| 2026-10-27 07:13:55 | ETHUSD | SELL | 173 | 2302.8 | $3,983.84 | $1.99 | `deriskexit` |
| 2026-10-27 07:12:30 | BTCUSD | BUY | 62 | 79595 | $4,934.89 | $2.47 | `deriskexit` |
| 2026-10-27 07:11:04 | PAXGUSD | BUY | 996 | 4013.37 | $3,997.32 | $0.40 | `deriskexit` |

_(All-time fills: **66** — showing the 25 most recent. Kind decoded from client_order_id.)_


## Strategy C — `credit_spread`

_Initial equity $100,000.00. Start 2026-10-23._

Equity **$100,000.00** · P&L **+$0.00** (+0.00%) · trading days **5** (green 0 / red 0) · max drawdown **+0.00%** · fees $0.00

- Closed lots: 0
- Open unrealized PnL: **+$0.00**
- Open positions: none
_(Option P&L, when applicable, is an approximate Black-Scholes model.)_

### PnL calendar
### October 2026

| Mon | Tue | Wed | Thu | Fri | Sat | Sun |
|---|---|---|---|---|---|---|
|   |   |   | · | · |   |   |
| · | · | · | · | · |   |   |
| · | · | · | · | · |   |   |
| · | · | · | · | **23** 0.00 | **24** 0.00 | **25** 0.00 |
| **26** 0.00 | **27** 0.00 | · | · | · |   |   |

_(UTC days. `**day** +$PnL` = recorded session day; `·` = weekday with
no session; weekends without data left blank. Future days left blank.)_

### Daily stats (last 30 recorded days)

| Date | Day PnL | Equity (MTM) | Cash | Unrealized | Trades | Fees | Sessions | Open positions |
|---|---|---|---|---|---|---|---|---|
| 2026-10-27 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 1 | — |
| 2026-10-26 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 4 | — |
| 2026-10-25 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 1 | — |
| 2026-10-24 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 2 | — |
| 2026-10-23 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 4 | — |

_(Day PnL: closing equity vs previous recorded day close; MTM includes
 unrealized PnL of open positions. Trades = fills journaled that day.)_

### Trades (last 25 fills, newest first)

_No fills journaled yet._


## Strategy A — `rwa_positional`

_Initial equity $100,000.00. Start 2026-10-22._

Equity **$105,883.85** · P&L **+$5,883.85** (+5.88%) · trading days **8** (green 4 / red 4) · max drawdown **-3.68%** · fees $87.44

- Closed lots: 0
- Open unrealized PnL: **+$5,971.29**
- Open positions: `PAXGUSD -37178`, `SLVONUSD -1361`, `USOONUSD -4956`, `XAUTUSD -15262`
_(Option P&L, when applicable, is an approximate Black-Scholes model.)_

### PnL calendar
### October 2026  — month +$5,883.85

| Mon | Tue | Wed | Thu | Fri | Sat | Sun |
|---|---|---|---|---|---|---|
|   |   |   | · | · |   |   |
| · | · | · | · | · |   |   |
| · | · | · | · | · |   |   |
| · | · | · | **22** -$843.73 | **23** +$6,963.64 | **24** -$538.18 | **25** -$3,365.19 |
| **26** +$2,119.75 | **27** +$276.12 | **28** -$1,277.37 | **29** +$2,548.81 | · |   |   |

_(UTC days. `**day** +$PnL` = recorded session day; `·` = weekday with
no session; weekends without data left blank. Future days left blank.)_

### Daily stats (last 30 recorded days)

| Date | Day PnL | Equity (MTM) | Cash | Unrealized | Trades | Fees | Sessions | Open positions |
|---|---|---|---|---|---|---|---|---|
| 2026-10-29 | +$2,548.81 | $105,883.85 | $99,912.56 | +$5,971.29 | 0 | $0.00 | 1 | PAXGUSD-37178, SLVONUSD-1361, USOONUSD-4956, XAUTUSD-15262 |
| 2026-10-28 | -$1,277.37 | $103,335.04 | $99,912.56 | +$3,422.49 | 0 | $0.00 | 3 | PAXGUSD-37178, SLVONUSD-1361, USOONUSD-4956, XAUTUSD-15262 |
| 2026-10-27 | +$276.12 | $104,612.41 | $99,912.56 | +$4,699.86 | 12 | $33.78 | 3 | PAXGUSD-37178, SLVONUSD-1361, USOONUSD-4956, XAUTUSD-15262 |
| 2026-10-26 | +$2,119.75 | $104,336.29 | $99,946.34 | +$4,389.95 | 9 | $14.32 | 3 | PAXGUSD-9404, SLVONUSD-1408, USOONUSD-4956, XAUTUSD-15262 |
| 2026-10-25 | -$3,365.19 | $102,216.54 | $99,960.66 | +$2,255.88 | 4 | $7.49 | 4 | PAXGUSD-28004, SLVONUSD-15282, USOONUSD-4956, XAUTUSD-15262 |
| 2026-10-24 | -$538.18 | $105,581.73 | $99,968.15 | +$5,613.58 | 2 | $1.32 | 4 | PAXGUSD-9286, SLVONUSD-15282, USOONUSD-4956, XAUTUSD-15262 |
| 2026-10-23 | +$6,963.64 | $106,119.91 | $99,969.47 | +$6,150.44 | 9 | $8.42 | 4 | PAXGUSD-9286, SLVONUSD-15282, USOONUSD-4956, XAUTUSD-11964 |
| 2026-10-22 | -$843.73 | $99,156.27 | $99,977.89 | -$821.62 | 8 | $22.11 | 3 | PAXGUSD-9286, SLVONUSD-7566, USOONUSD-4956 |

_(Day PnL: closing equity vs previous recorded day close; MTM includes
 unrealized PnL of open positions. Trades = fills journaled that day.)_

### Trades (last 25 fills, newest first)

| Time (UTC) | Symbol | Side | Size | Price | Notional | Fee | Kind |
|---|---|---|---|---|---|---|---|
| 2026-10-27 14:44:08 | PAXGUSD | SELL | 18546 | 4052.23 | $75,152.66 | $7.52 | `bracket` |
| 2026-10-27 14:44:08 | PAXGUSD | SELL | 13896 | 4052.23 | $56,309.79 | $5.63 | `bracket` |
| 2026-10-27 14:44:08 | PAXGUSD | SELL | 9255 | 4052.23 | $37,503.39 | $3.75 | `bracket` |
| 2026-10-27 14:43:42 | PAXGUSD | SELL | 4623 | 4052.56 | $18,734.98 | $1.87 | `bracket` |
| 2026-10-27 14:43:42 | PAXGUSD | BUY | 4650 | 4054.49 | $18,853.38 | $1.89 | `rung4` |
| 2026-10-27 14:43:42 | PAXGUSD | BUY | 4641 | 4054.49 | $18,816.89 | $1.88 | `rung3` |
| 2026-10-27 14:43:42 | PAXGUSD | BUY | 4632 | 4054.49 | $18,780.40 | $1.88 | `rung2` |
| 2026-10-27 14:43:39 | PAXGUSD | BUY | 4623 | 4054.49 | $18,743.91 | $1.87 | `rung1` |
| 2026-10-27 05:45:11 | SLVONUSD | BUY | 3883 | 48.27 | $18,743.24 | $1.87 | `rung1` |
| 2026-10-27 05:30:19 | SLVONUSD | BUY | 3912 | 47.92 | $18,746.30 | $1.87 | `rung1` |
| 2026-10-27 05:22:20 | SLVONUSD | SELL | 3874 | 48.32 | $18,719.17 | $1.87 | `rung1ioc` |
| 2026-10-27 05:21:06 | SLVONUSD | SELL | 3874 | 48.35 | $18,730.79 | $1.87 | `rung1` |
| 2026-10-26 15:30:07 | SLVONUSD | BUY | 2130 | 49.04 | $10,445.52 | $1.04 | `rung1` |
| 2026-10-26 15:22:13 | SLVONUSD | BUY | 2061 | 49.14 | $10,127.75 | $1.01 | `rung1ioc` |
| 2026-10-26 15:22:13 | PAXGUSD | BUY | 4646 | 4035.66 | $18,749.68 | $1.87 | `rung1ioc` |
| 2026-10-26 15:20:56 | SLVONUSD | BUY | 2061 | 49.11 | $10,121.57 | $1.01 | `rung1` |
| 2026-10-26 15:20:54 | PAXGUSD | BUY | 4646 | 4034.86 | $18,745.96 | $1.87 | `rung1` |
| 2026-10-26 05:06:44 | SLVONUSD | BUY | 3811 | 49.21 | $18,753.93 | $1.88 | `rung1ioc` |
| 2026-10-26 05:06:44 | PAXGUSD | BUY | 4654 | 4027.83 | $18,745.52 | $1.87 | `rung1ioc` |
| 2026-10-26 05:05:28 | SLVONUSD | BUY | 3811 | 49.2 | $18,750.12 | $1.88 | `rung1` |
| 2026-10-26 05:05:27 | PAXGUSD | BUY | 4654 | 4028.48 | $18,748.55 | $1.87 | `rung1` |
| 2026-10-25 19:16:06 | PAXGUSD | SELL | 4675 | 4008.52 | $18,739.83 | $1.87 | `rung1ioc` |
| 2026-10-25 19:15:13 | PAXGUSD | SELL | 4675 | 4008.53 | $18,739.88 | $1.87 | `rung1` |
| 2026-10-25 13:17:58 | PAXGUSD | SELL | 4684 | 3999.53 | $18,733.80 | $1.87 | `rung1ioc` |
| 2026-10-25 13:16:39 | PAXGUSD | SELL | 4684 | 3999.53 | $18,733.80 | $1.87 | `rung1` |

_(All-time fills: **44** — showing the 25 most recent. Kind decoded from client_order_id.)_

