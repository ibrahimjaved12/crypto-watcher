# Pattern event study: candle-table-v1

Candle patterns are studied as **events over a complete candle table**, not as hand-scored
detectors. `market_analysis.patterns.candle_table` builds one row per symbol x timeframe x
completed candle for **every** candle, whether or not it is a pattern hit. A pattern is then
a filter over the table's columns (for example `doji_b10 = 1 and size_ge_05 = 1`). Its
outcomes are compared with the rows that do not match, conditioned on the same context
columns (trend, volatility, volume, regime). The table is descriptive: it holds no signal and
nothing from it enters the experiment ledger by itself.

doji-v1 ([candle-patterns.md](candle-patterns.md)) stays frozen as an exploratory baseline.
This study supersedes it.

## Table

| Timeframes | Source | Rows | Segments |
|---|---|---|---|
| 15, 60, 240 | rd 1-minute bars, `build_candles` | 2024-01 .. through_month | development (to 2025-06), validation (2025-07..2025-12) |
| 1440, 10080 | dk1 daily klines, `candles_from_daily` | 2021-01 .. through_month | development-ext (2021-01..2025-06), validation |

- **Warm-up:** 2020 dk1 data is used only to warm up indicators. Intraday indicators warm
  up inside 2024-01; those rows are marked `insufficient`, never filled.
- **`through_month`:** defaults to 2025-12 and may not be later. The hidden guard runs over
  every month before any file is opened, so the hidden stretch is never read.
- **Files:** one `candles__SYMBOL__TF__FIRST_LAST.csv.gz` per symbol and timeframe. The
  header comes first and rows are sorted by open time. Floats are written with `repr`,
  booleans as 1/0, and unavailable values as an empty cell.
- **Manifest:** `manifest.json` (schema `candle-table-v1`) holds:
  - the group versions and every column with its one-line doc;
  - row counts per (symbol, tf, status, segment) and `fwd_status` counts;
  - input release tags and sha256s, the git sha and `through_month`;
  - the dk1-vs-1m `daily_parity` per symbol;
  - a content hash that leaves out the commit and creation time.
- **Storage:** everything is written to `reports/candle-table/ct1__<hash16>/` in the private
  research-data repo by `.github/workflows/candle-table-build.yml`. A directory with the same
  hash is skipped. The public log carries counts and the hash only.

`status` is `ok`, `invalid` or `insufficient`:

- **invalid:** the candle is price-invalid. Only the keys are filled.
- **insufficient:** a feature group lacks history or data. `reasons` names the groups.
- Outcomes never affect `status`; they have their own `fwd_status`.

## Point-in-time rules

- **Decision time:** the candle's `end_ms`. Columns named `*_prev` (and `trend_dir`,
  `taker_ratio_prior20`) read candles through `i - 1` only. Plain names may read candle `i`.
  Ratios in `*_prev` columns divide by the sigma at the end of `i - 1`, so they read
  nothing of candle `i`.
- **Outcomes:** they read candles `i + 1 .. i + m` only. The window must be price-valid and
  end inside the segment that contains the decision time. The exit is the close of `i + m`,
  never the next open, which for the last segment would read the hidden stretch.
- **Regime:** the join takes the regime-v1 row of the UTC day containing the decision time.
  A decision at midnight takes the new day, whose labels use closes through the previous day.
- **Empty means unavailable.** Nothing is forward-filled. Geometry ratios and doji/size flags
  use exact integer differences (thresholds compared as integers).

## Groups

| Group | Version | Columns (summary) |
|---|---|---|
| keys | keys-v1 | symbol, tf, open_ms, decision_ms, segment, status, reasons |
| geometry | geo-v1 | OHLC, range/body/wicks and their ratios, color, atr_prev (Wilder ATR14 through i-1), range_atr, body_atr, doji_b05, doji_b10, doji_subtype, size_ge_05, size_ge_10 |
| trend_vol | tv-v1 | EMA20/50, trend_dir, ema_gap_atr_prev, ADX/+DI/-DI, extension_prev_atr, streak_prev, sigma_tf, sigma_model, ret1/ret3_prev_sigma, atr_pct_prev, bb_width_pct_prev, squeeze_on_prev, squeeze_bars_prev |
| volume | vol-v1 | volume, rel_volume_prior20, rel_volume_slot (intraday), taker_buy_ratio, taker_ratio_prior20, taker_imb |
| calendar | cal-v1 | utc_dow; intraday: utc_hour, is_weekend, session_bin, funding_in_candle, mins_to_funding |
| regime | regime-v1 | `rg_` + every market and symbol column of `regime.build_table` (built from dk1 inside the run) |
| outcomes | fwd-v1 | for m in 1, 2, 4: ret_m_bp, ret_m_sigma, mfe/mae_long_m_sigma, fwd_ok_m; range1_sigma, absret1_sigma, ln_range1_sigma, fwd_status |

**Sigma:**

- **Intraday** (`ewma-3d-5m-v1`): the screen.py rule,
  `horizon_sigma(build_variance(bars, 3).variance[block], tf) / VAR_SCALE`, with the block
  whose last minute is the candle's last minute.
- **Daily** (`ewma-30d-daily-v1`): a bias-corrected EWMA of squared daily log returns.
  - Half-life is 30 days, and the variance runs through day `i` inclusive.
  - It is published after 30 returns; a day without a return changes nothing.
- **Weekly:** the daily sigma at the week's Sunday times `sqrt(7)`.

**Percentiles:** `atr_pct_prev` and `bb_width_pct_prev` are the fraction of the last W values
(W = 2880, 720, 180, 90, 52 for 15m..1w) that are <= the value at `i - 1`. The window must be
complete.

**Short side:** there are no extra columns. The short MFE is `mae_long`, the short MAE is
`mfe_long`, and the short return is `-ret`.

Every feature group has `<group>_ok` and `<group>_missing`. A column that does not apply to
a row is left empty and does not count as missing. Examples are `doji_subtype` outside
`doji_b10` and the intraday-only calendar and volume columns on daily/weekly rows.

## Adding a group (location, derivatives, advanced levels, ...)

1. Write `build(ctx, i) -> dict` in `candle_table.py` or a sibling module. It reads only
   `TableContext` data and obeys the point-in-time rules above. Return `None` for a missing
   value, and leave a column out of the dict when it does not apply.
2. Declare the columns as `(name, one-line doc)` and register them with
   `REGISTRY.register(Group(id, version, columns, build))`. Duplicate group ids or column
   names, including the flag columns, are rejected.
3. Add hand-computed tests to `python/tests/test_candle_table.py`: boundaries, a look-ahead
   guard, and missing/not-applicable behaviour. Then rebuild the table. The new group
   version enters the manifest and the content hash, so the build lands in a new directory.

## Adding a pattern

A pattern is a **filter over existing columns** plus a pre-registered comparison. It is not
new detector code. If the pattern needs a quantity the table lacks, first add it as a column
of a new group version (above). Then:

1. Define the filter.
2. Choose the matched comparison rows.
3. Choose the outcome columns.

Write all three in the plan before looking at validation rows.
