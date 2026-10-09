# SPEC-5: Data sources, schemas and point-in-time rules

*Part of the strategy specification library (#219). 2026-10-09. Draft by Claude (AI). Owner direction: sufficient free, good data for 2024-01..2026-09 from more than one source (Binance is not the only venue; OKX is usable from Pakistan); 2020-2023 is a fallback for multi-day families. Items marked VERIFY come from research or secondary sources and must be checked against the exchange or a downloaded file before code relies on them. Paid data needs explicit owner approval.*

## 1. What we have (private research-data repo, `crypto-watcher-research-data`)

- Six symbols (BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, DOGEUSDT, XRPUSDT), core window 2024-01..2026-09, from the Binance public archive `data.binance.vision` (monthly zips for finished months): 1-minute klines (OHLCV, quote volume, trade count, taker buy base/quote volume), aggTrades (tick trades with taker side), funding rate history (8h), mark-price, index-price and premium-index 1-minute klines. Release names `rd-SYMBOL-YYYY-MM-rN`; trade labels `lb1-SYMBOL-FIRST_LAST-rN` (k in {1, 2}, rr in {1, 1.5, 2, 3}, horizons 15/60/240, step minutes {15: 5, 60: 15, 240: 15}); `rk-` reserved for a 2020-2023 extension.
- Klines, mark/index/premium and funding could extend back to 2020 (aggTrades stay in the core window).

## 2. Acquisition list (priority order; tickets #224 and #228)

| # | Dataset | Source (free) | Coverage / granularity | Needed by | Notes |
| --- | --- | --- | --- | --- | --- |
| 1 | **Binance metrics archive**: open interest, top-trader long/short account and position ratios, global long/short ratio, taker buy/sell volume ratio | `data.binance.vision/data/futures/um/daily/metrics/<SYMBOL>/` daily zips | 5-minute rows; start around late 2021 for the six symbols (VERIFY by opening one file; BTCUSDT may start 2020-09) | #188 phase 2, regime/leverage filters | Columns (VERIFY): `create_time, symbol, sum_open_interest, sum_open_interest_value, count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio, sum_taker_long_short_vol_ratio`. Archive lags about 1.5-2 days (research only). REST `openInterestHist` and `/futures/data/*` keep only 30 days: start logging live now. |
| 2 | **Second venue (OKX)**: tick trades, candlesticks, funding, L2 order book, open interest and long/short statistics, liquidation orders where available | okx.com/historical-data page (trades from 2021-09, candles from 2023-07, funding from 2022-03, order book from 2023-03; cost/login/limits VERIFY) and the OKX REST API (history windows limited, VERIFY) | per audit in #228 | replication venue, redundancy, possible execution venue | OKX contract identity (BTC-USDT-SWAP, contract value, settlement currency, funding interval) kept explicit; never mix venue prices in one series. |
| 3 | **Spot 1-minute klines** (six symbols) | `data.binance.vision/data/spot/...` | 2020+ | basis, spot-led flow, independent premium | spot-hedged strategies are out of scope; used as a feature/cross-check |
| 4 | **Deribit DVOL** (BTC, ETH 30-day implied vol index) | public API `public/get_volatility_index_data` (no auth) | history from about 2021 | regime filter, implied minus realized | free |
| 5 | **Forward liquidation recorder** | Binance websocket `!forceOrder@arr`; OKX public liquidation orders if available | forward only | cascade research | Throttled sample ("only the latest liquidation per symbol per 1000 ms"; wording changed April 2026), so a lower bound. `allForceOrders` REST discontinued in 2021; archive `liquidationSnapshot` reported discontinued (VERIFY). Full history is paid (Tardis.dev, Coinglass API). |
| 6 | **Order-book depth / bookTicker** | `data.binance.vision` (`bookDepth`, `bookTicker`) | UM availability and continuity uncertain (bookTicker reportedly stalled around 2024; VERIFY) | order-book imbalance (strong at seconds-minutes, weak at our horizons after costs) | low priority; OKX L2 from 2023-03 is a possible alternative if huge volumes are manageable |
| 7 | **Daily/weekly/monthly derived bars and level tables** | derived from 1-minute bars | UTC | levels, trend, regime | computed in the lake builder; versioned |
| 8 | **Regime/season table** | derived (#223) | daily | context | frozen definitions |
| 9 | **Fear & Greed index**, **spot-ETF flow tables**, stablecoin supply | alternative.me API (`/fng/?limit=0`), Farside tables, DefiLlama | daily | daily filters, later | grade C; free |
| 10 | **Macro event calendar** (FOMC, CPI), **SPX/NDX/DXY/gold** | Federal Reserve and BLS schedules; FRED, Stooq | daily/event | volatility blackout filter, macro regime | news stays out |
| 11 | **BTC daily history before 2020** | external public sources | 2013+ | descriptive season/halving statistics only | not in the harness; never a signal source |
| 12 | Paid (only with owner OK) | Tardis.dev (full liquidation/book tapes), Coinglass API, Glassnode/CryptoQuant | - | liquidation history, on-chain | defer |

## 3. Point-in-time rules

- All times UTC milliseconds. Bars are labeled by open time; a bar is usable only after it closes. A signal at the close of minute `d` enters at the open of minute `d+1`.
- **Metrics archive timestamps:** establish from Binance documentation and a sample file whether `create_time = t` describes the 5-minute period ending at `t`; until verified, join with a conservative extra lag of one 5-minute period. Document the rule in code and docs.
- Derived higher-timeframe bars (4h, 1D) use completed bars only; "previous day/week" levels are available only after that period closes; fractal swings only after `n` confirming bars.
- Funding: the rate for a settlement is known at settlement; premium-based predicted funding is known continuously. Settlement `calc_time_ms` can be a few ms off the minute: floor to the minute. Funding recompute check: TWAP of premium with weights 1..N, `F = P + clamp(0.0001 - P, -0.0005, +0.0005)`, median absolute error 0.03-0.06 bp using OHLC4 or close.
- Mark vs last: liquidation is judged on mark price; stops on last price; keep both series.
- No smoothed or revised labels (regime, HMM filtered probabilities only); no data from the future in normalizations (expanding or trailing windows).

## 4. Quality checks (each dataset ships with a coverage report)

Gaps and duplicates; zero or repeated values (stuck feeds); unit changes (contracts vs coins vs USD; contract multiplier changes); symbol listing dates (SOL, DOGE, XRP, BNB differ from BTC/ETH); timestamp monotonicity and daylight/rollover handling; checksum verification for every zip; row counts per day (1,440 for 1-minute, 288 for 5-minute); compromised-minute flags (existing C status); cross-venue price difference stability (OKX vs Binance) as a sanity check.

## 5. Release and storage conventions

Private repo, GitHub Actions builder (`data-lake-build.yml` pattern: dry run `publish=false` first), assets below 2 GiB, raw zips mirrored with checksums, derived datasets as separate releases, never marked Latest, readable titles. Proposed prefixes: `mx-SYMBOL-YYYY-MM-rN` (metrics), `ok-SYMBOL-YYYY-MM-rN` (OKX), `sp-SYMBOL-YYYY-MM-rN` (spot) pending owner OK. The public repo holds only harness code; results, counts and experiment logs stay private.


---
_Generated by [Claude Code](https://claude.ai/code)_
