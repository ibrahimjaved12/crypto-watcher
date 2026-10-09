# Binance metrics archive (phase 1)

> **Provisional.** This document is AI-generated and is not the source of truth. The owner's
> intent in [`CLAUDE.md`](../CLAUDE.md) takes precedence. Ticket:
> [#224](https://github.com/ibrahimjaved12/crypto-watcher/issues/224).

## Dataset

Binance publishes a daily `metrics` file for each USDⓈ-M perpetual. Each file holds one row
per 5 minutes: open interest (contracts and value), top-trader long/short ratios
(accounts and positions), the global long/short account ratio, and the taker buy/sell
volume ratio. Phase 1 archives these files for the six frozen symbols. The source is:

`https://data.binance.vision/data/futures/um/daily/metrics/<SYMBOL>/<SYMBOL>-metrics-YYYY-MM-DD.zip`

Each zip comes with a `.CHECKSUM` file.

Nobody has verified the column layout yet, because the development sandbox cannot reach
the archive. The parser (`python/market_analysis/metrics_lake.py`) therefore works from
the header:

- **Header:** the first line must be `create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio`.
  Any other header stops the build, and the error shows the header that was found.
- **Timestamps:** `create_time` may be a `YYYY-MM-DD HH:MM:SS` UTC string or epoch
  milliseconds. Both are stored as epoch milliseconds, and the manifest records which
  format was seen.
- **Values:** stored exactly as published and checked to be decimals. Empty values are
  kept and counted.

The first dry run reports the real header, the rows per day and the timestamp format.

## Building and release layout

The **Metrics build** workflow (`.github/workflows/metrics-build.yml`, script
`scripts/research/metrics_build.py`) runs one job per symbol x finished month, at most
three at a time. It accepts months from 2020-01; the default window is 2024-01..2026-09.

- **Missing days:** a day whose file returns HTTP 404 is recorded as a gap, not an error.
- **Errors:** a checksum mismatch or an unexpected header stops the build.
- **Empty months:** a month with no daily file at all is reported as empty and never
  published.

With `publish=true`, each symbol-month becomes one immutable release in the private
research-data repo, tagged `mx-SYMBOL-YYYY-MM-rN`. It is never marked Latest and has a
readable title. It holds:

| Asset | Contents |
| --- | --- |
| `metrics__SYMBOL__YYYY-MM.csv.gz` | Epoch-ms timestamp plus the published values as exact text, sorted by timestamp, one row per timestamp |
| `metrics-raw__SYMBOL__YYYY-MM.tar` | The original daily zips, unmodified |
| `metrics__SYMBOL__YYYY-MM.manifest.json` | Per day: source sha256, verified checksum, row count, rows outside the day and empty values. Per month: header, timestamp format, missing days and coverage statistics. |

Existing releases are never edited or deleted. Download, checksum, upload and publish use
the data-lake builder's code. The `rd-` data lake identities and tag rule are unchanged.

## Point-in-time rule

Until the first dry run proves otherwise:

- A row stamped T describes the 5-minute period ending at T.
- A row is used only by decisions at T + 5 minutes or later, one extra period of lag for
  publication delay.

The rule lives in the constants `PERIOD_MS` and `POINT_IN_TIME_LAG_PERIODS` and in
`usable_from_ms`. It is deliberately conservative. The loader `load_symbol_metrics` asks
the hidden guard to authorize every requested month before it opens any file, so hidden
months need a verified opening token.

## Coverage report

For each symbol-month the manifest and the public job summary show only aggregates:

- days present and missing;
- rows per day (minimum, maximum, and days without 288 rows);
- duplicate and non-monotonic timestamps, and duplicates with conflicting values;
- zero or negative open interest, and empty open-interest values;
- the longest run of an identical `sum_open_interest` value, which points to a stuck feed;
- day-over-day open-interest jumps above 5x or below 0.2x, which point to a unit change;
- the first and last timestamp.

The data is a public archive and this repository is public, so the logs carry only names,
sizes, hashes, the header text and these aggregates.

## Deferred

- **Live logging and the liquidation recorder:** these wait for the owner's decision D3
  and a hosting decision.
- **CLAUDE.md release naming:** it does not yet list the `mx-` prefix. Adding it is the
  owner's edit and is not made here.
