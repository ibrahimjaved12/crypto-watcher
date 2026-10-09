# Daily bars and funding history (dk1)

> **Provisional.** This document is AI-generated and is not the source of truth. The owner's
> intent in [`CLAUDE.md`](../CLAUDE.md) takes precedence. Tickets:
> [#222](https://github.com/ibrahimjaved12/crypto-watcher/issues/222),
> [#223](https://github.com/ibrahimjaved12/crypto-watcher/issues/223).

## Purpose: warm-up only

The multi-day trend ensemble (#222) uses lookbacks of up to 360 days, and the regime labels
(#223) use SMA200 and a 365-day volatility window. The 1-minute data lake starts in 2024-01,
so on its own every one of those indicators would be undefined for most of the development
segment.

dk1 adds daily bars and funding settlements from 2020 onward. It serves **indicator warm-up
and context only**:

- The evaluation segments do not change: development 2024-01..2025-06, validation
  2025-07..2025-12, hidden 2026-01..2026-09.
- Nothing is tuned or evaluated on pre-2024 data.
- Owner decision D2 keeps 2020-2023 history as a fallback for evaluation. Using it for
  warm-up does not make it evaluation data.

## Sources and release layout

For each month, the **Daily build** workflow (`.github/workflows/daily-build.yml`, script
`scripts/research/daily_build.py`) downloads two monthly zips from the Binance USDⓈ-M
archive, each with its `.CHECKSUM`:

- `klines/<SYMBOL>/1d`
- `fundingRate`

A month that returns HTTP 404 is a recorded gap; a symbol listed later simply starts
later. A checksum mismatch is an error. Each job covers one symbol and the whole month
range. Months from 2019-09 are accepted; the default range is 2020-01..2026-09.

With `publish=true`, each symbol becomes one immutable release `dk1-SYMBOL-FIRST_LAST-rN`
in the private research-data repo. It is never marked Latest and has a readable title. It
holds:

| Asset | Contents |
| --- | --- |
| `daily__SYMBOL__FIRST_LAST.csv.gz` | `open_time_ms,open,high,low,close,volume,quote_volume,trades,taker_buy_volume,taker_buy_quote_volume`, values as published, sorted by day |
| `funding__SYMBOL__FIRST_LAST.csv.gz` | The lake's four funding columns, one header. Each month is validated by the lake's own funding writer. |
| `daily-raw__SYMBOL__FIRST_LAST.tar` | The original monthly zips, unmodified |
| `daily__SYMBOL__FIRST_LAST.manifest.json` | Per-file URL, sha256, verified checksum and row counts, plus the coverage statistics |

Parsing rules:

- **Open time:** must be a UTC day start, else the build fails naming the file and line.
- **Rows outside the zip's month:** dropped and counted.
- **Duplicates:** the first row wins, and conflicting duplicates are counted.

Existing releases are never edited or deleted. The `rd-` data lake identities and tag rule
are unchanged.

## Coverage statistics

Each manifest reports the following for each symbol over the whole range:

- **Range:** the first and last day present (the first day is the listing date), the days
  expected between them, and the list of missing days.
- **Data quality:** duplicate rows and conflicts, rows outside their month, days with high
  below low, close outside [low, high], non-positive prices, and zero-volume days.
- **Largest move:** the largest absolute close-to-close daily log return and its date.
- **Funding:** the number of settlements, the funding intervals seen, and gaps between
  settlements longer than 9 hours (count and longest).

The public job summary shows only names, sizes, hashes and these aggregates.

## Loader contract

```python
load_symbol_daily(daily_dir, symbol, first_month, last_month, *, token=None, gate=None) -> DailySeries
load_symbol_funding_range(daily_dir, symbol, first_month, last_month, *, token=None, gate=None) -> FundingSeries
```

- **Hidden guard:** `hidden_guard.require_months` authorizes every requested month before
  any file is opened, so hidden months need a verified opening token.
- **Files with hidden months:** a release file also holds hidden months (it runs to
  2026-09). The loaders stop reading at the first row after the requested range, so later
  rows are never interpreted.
- **`DailySeries`:** holds `symbol`, `start_ms`, `days`, `open`, `high`, `low`, `close`,
  `volume`, `taker_buy_volume`, `open_time(i)` and `end_ms`.
  - Prices and quantities are `array('q')` scaled by 10^8.
  - The series starts at the first present day in the range, so days before a listing do
    not exist.
  - A gap day holds `bars.MISSING` in every column.
- **`FundingSeries`:** the benchmark's own funding type, covering the requested months
  exactly.
