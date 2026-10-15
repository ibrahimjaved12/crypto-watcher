# Kline lake (rk-): 1-minute bars before 2024-01

Owner decision 2026-10-15: intraday development extends back to 2021-01 on 1-minute bars built
from the Binance monthly 1m klines. aggTrades (the rd- lake) stay 2024-01 onward. Validation
(2025-07..2025-12) and hidden (2026-01..2026-09) are unchanged.

## Releases

`rk-SYMBOL-YYYY-MM-r1` for the frozen six and 2020-12..2023-12 (2020-12 is indicator warm-up),
built by `.github/workflows/klines-build.yml` / `scripts/research/klines_build.py`. Run it with
`publish=false` first. Each release holds the four raw monthly zips (klines, markPriceKlines,
indexPriceKlines, premiumIndexKlines), each verified against its `.CHECKSUM` (a mismatch stops the
run), `bars1m__SYMBOL__MONTH.csv.gz` and `manifest.json` (uploaded last). It is published from a
draft, never marked Latest, and never edited or deleted. Funding for these months comes from the
dk1 history, so rk- has no funding asset.

## Bars

The same `bars1m-v1` columns as rd-, so every existing reader (`read_bars_csv`, premium/basis)
loads them unchanged:

- open/high/low/close, volume, quote volume, taker volumes and trades come from the kline row;
  `agg_rows` is 0 and the agg ids are empty; `k_*` are the kline row as published; mark/index/premium
  OHLC are as published.
- Every minute carries `KLINE_SOURCE` (flag 256, `data_lake.RK_FLAG_BITS`).
- A minute without a kline row gets `KLINE_ROW_MISSING | NO_AGGTRADES` and empty prices. A kline
  with zero trades gets `NO_AGGTRADES` and empty prices, because Binance repeats the previous close
  there and that is not a traded price. Either way the candle containing the minute is invalid,
  exactly like a minute without aggTrades in rd-. Nothing is filled.

## Parity at the boundary

`parity=true` builds 2024-01 from the klines per symbol (never published) and compares it with the
published rd 2024-01 bars (`data_lake.compare_bars`). The job summary shows the minutes compared,
priced-minute mismatches, minutes whose open/high/low/close, volume, taker volume or trades differ,
the largest relative OHLC difference in bp, and the month volume totals. Only counts and aggregates
are printed.

## Use

The candle-table build loads rk- 2020-12..2023-12 and rd- 2024-01..through_month as one
contiguous series (`BarSeries.concat` refuses a gap or an overlap). Intraday rows from 2021-01
form the segment `development-ext` with `bar_source = kline` (docs/pattern-study.md).
