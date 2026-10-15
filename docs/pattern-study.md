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
  - `insufficient_reason_counts` (symbol|tf|segment|`reasons` value) and `insufficient_by_month`
    (symbol|tf|YYYY-MM of the decision time), so clusters show without opening the files;
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
  m-candle quantities (`ret_m_sigma`, `mfe/mae_long_m_sigma`) are in one-candle sigma units;
  divide by `sqrt(m)` to compare across m. Likewise `ret3_prev_sigma` is not divided by `sqrt(3)`.
- **Regime:** the join takes the regime-v1 row of the UTC day containing the decision time.
  A decision at midnight takes the new day, whose labels use closes through the previous day.
- **Empty means unavailable.** Nothing is forward-filled. Geometry ratios and doji/size flags
  use exact integer differences (thresholds compared as integers).

## Groups

| Group | Version | Columns (summary) |
|---|---|---|
| keys | keys-v1 | symbol, tf, open_ms, decision_ms, segment, status, reasons |
| geometry | geo-v1 | OHLC, range/body/wicks and their ratios, color, atr_prev (Wilder ATR14 through i-1), range_atr, body_atr, doji_b05, doji_b10, doji_subtype, size_ge_05, size_ge_10 |
| trend_vol | tv-v2 | EMA20/50, trend_dir, ema_gap_atr_prev, ADX/+DI/-DI, extension_prev_atr, streak_prev, sigma_tf, sigma_model, ret1/ret3_prev_sigma, atr_pct_prev, bb_width_pct_prev, squeeze_on_prev, squeeze_bars_prev |
| volume | vol-v1 | volume, rel_volume_prior20, rel_volume_slot (intraday), taker_buy_ratio, taker_ratio_prior20, taker_imb |
| calendar | cal-v1 | utc_dow; intraday: utc_hour, is_weekend, session_bin, funding_in_candle, mins_to_funding |
| regime | regime-v1 | `rg_` + every market and symbol column of `regime.build_table` (built from dk1 inside the run) |
| location | loc-v1 | per level set: dist_up/dist_dn (ATR units), touch, approach_side; any_touch, nearest level/set, level_count_near, touch_approach_side, ATH/ATL distances, new_ath_in_candle |
| sweep | sweep-v1 | per reference (prior20, swing_tf, prev_day, prev_week, prev_month): sweep_up/sweep_dn; any, both, direction, depth |
| htf | htf-v1 | per higher timeframe (1h, 4h, 1d, 1w): trend_dir, adx, extension_atr, ema_gap_atr of the last closed higher candle |
| levels2 | lv2-v1 | liquidity pools (distance, touches, touch, swept), anchored VWAP from the last swing low/high, intraday day VWAP, 30d/90d volume profile POC/VAH/VAL |
| derivatives | deriv-v1 | funding (last, 90-day percentile, clamp flag, last-3 sum), premium/basis, open interest (end, start, change, z, OI/turnover), positioning (global, top-trader account/position, taker, global z) |
| flush | flush-v1 | flush_down, flush_up, flush_dir (price beyond 3 sigma with an OI collapse) |
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
(W = 2880, 720, 180, 90, 52 for 15m..1w) that are <= the value at `i - 1`. Since tv-v2 the rank is over the values that are present in the window
(an invalid candle is skipped, not filled), and it is empty when fewer than 80% of the W values are present
(`MIN_RANK_FRACTION`), when the value at `i - 1` is missing, or during warm-up (fewer than W candles).
tv-v1 required all W values, so one invalid candle blanked the next W candles (about 30 days on 15m).

**Short side:** there are no extra columns. The short MFE is `mae_long`, the short MAE is
`mfe_long`, and the short return is `-ret`.

Every feature group has `<group>_ok` and `<group>_missing`. A column that does not apply to
a row is left empty and does not count as missing. Examples are `doji_subtype` outside
`doji_b10` and the intraday-only calendar and volume columns on daily/weekly rows.

### Location (loc-v1)

**Level sets.** The sets are `benchmark/levels.py` `PATTERN_LEVEL_SETS`, in this fixed
order, which is also the tie-break order: round, prev_day, prev_week, prev_month,
swing_tf, swing4h, swing1d, swing1w. `LEVEL_SETS` and the level study are unchanged.

| Set | Source |
|---|---|
| round | per-symbol grid (`ROUND_SPACING`) |
| prev_day | high/low of the previous complete, fully valid UTC day (1m bars), valid during the next day |
| prev_week, prev_month | high/low of the previous complete, fully valid Monday week / calendar month from the dk1 daily candles, valid during the next period; the same lists serve intraday, daily and weekly rows (so the first table month already has a prev_month) |
| swing_tf | fractal on the row's own timeframe, n = 5, valid 7/30/90/180/365 days for 15m/1h/4h/1d/1w |
| swing4h | the level study's 4h swings (intraday rows only) |
| swing1d | daily candles, n = 5, valid 180 d |
| swing1w | weekly candles, n = 3, valid 365 d |

swing1d and swing1w come from dk1, so they exist for every row, intraday included.

**Not-applicable sets.** These are left empty and do not count as missing:

- prev_day on daily and weekly rows, and prev_week on weekly rows. At the decision time
  these would be (or contain) the row's own candle.
- swing4h on daily and weekly rows.

**Usability.** A level is usable for candle i iff it was valid when the candle opened:
`valid_from_ms <= open_ms(i) < valid_to_ms`. A period level therefore never contains the
candle's own high/low (the last 4h candle of day D sees day D-1's prev_day, not D's). A swing
is known `n` candles after its candle, so a candle is never its own swing. The candle's own
high, low, close and `atr_prev` are still used to measure distances and touches.

**Per-set columns.** With `atr = atr_prev`:

- `dist_up_S_atr` / `dist_dn_S_atr`: distance to the nearest level >= / <= close. The value
  is empty when the set has no level on that side.
- `touch_S`: a level lies in `[low - 0.25 atr, high + 0.25 atr]`.
- `approach_side_S`: for the nearest touched level (ties go to the lower price), +1 if
  `close_{i-1}` is below it (approaching resistance), -1 if above (approaching support),
  0 if equal. Empty when nothing was touched.

**Overall columns:**

- `any_touch`.
- `nearest_level_dist_atr` and `nearest_set`, with ties broken by set order.
- `level_count_near`: distinct prices within 0.5 atr of close.
- `touch_approach_side`: the approach side of the nearest touched level of any set.

**ATH/ATL.** `dist_ath_atr`, `dist_atl_atr` and `new_ath_in_candle` use the highest high /
lowest low since 2020-01-01 (the start of the dk1 history), which is not necessarily the
all-time extreme. Intraday rows combine the valid dk1 days that end at or before the UTC day
start of the candle's open with the valid intraday candles of that day through candle i.
Daily and weekly rows use the valid dk1 days that start before the candle's end (so a weekly
row includes the days of its own week). `new_ath_in_candle` compares `high_i` with the
extreme strictly before candle i. Invalid days and candles are skipped.

### Sweep (sweep-v1)

**References.** prior20 (max high / min low of candles i-20..i-1, all valid), plus the
swing_tf, prev_day, prev_week and prev_month levels that were valid when the candle opened.
Applicability follows the location rules.

**Rules:**

- `sweep_up_X`: `high_i > H + 0.1 atr_prev` and `close_i < H` for a reference high H.
- `sweep_dn_X`: the mirror rule, using lows.

**Summary columns:**

- `sweep_any_up`, `sweep_any_dn`.
- `sweep_both` (outside bar).
- `sweep_dir`: -1 swept up only, +1 swept down only, 0 both or none.
- `sweep_depth_atr`: the largest piercing depth. Empty, and not missing, when nothing was swept.

### Higher timeframes (htf-v1)

| Row | Higher timeframes |
|---|---|
| 15m | 1h, 4h |
| 1h | 4h, 1d |
| 4h | 1d, 1w |
| 1d | 1w |
| 1w | none |

1h and 4h come from the 1m bars; 1d and 1w come from dk1.

Each row uses the **last higher candle that closed at or before the row candle's open**,
so the higher state never includes the row's own candle (a 15m candle deciding exactly at a
4h close does not see that 4h candle; the next 15m row does). Its state is taken through its own close, using
`CandleFeatureHistory.indicators` (inclusive):

- `trend_dir_H`: sign(EMA20 - EMA50).
- `adx_H`.
- `extension_H_atr`: (close - EMA20) / ATR14.
- `ema_gap_H_atr`: (EMA20 - EMA50) / ATR14.

Columns of timeframes that are not higher than the row's are not applicable.

location, sweep and htf never change `status`. Their availability is reported by
`<group>_ok` and `<group>_missing` only.

### Levels 2 (lv2-v1)

All columns follow the open-time rule: a level is used only if it was known when the row
candle opened. Signed distances are `(level - close) / atr_prev`.

**Liquidity pools (equal highs/lows).**

- Swings: fractal swings with n = 3. Rows of 15m/1h/4h use the dk1 daily candles; 1d/1w rows
  use their own candles.
- Clustering: a newly confirmed swing joins every active swing of the same side within
  0.15 × ATR14 of that series. ATR14 is taken through the confirming candle.
- Pools: two or more members form a pool at their mean. `pool_touches_*` is the member count.
  The pool is known at the end of the candle that confirms its latest member.
- Invalidation: a swing or pool ends after a source close beyond it by more than 0.1 × ATR14,
  or 90 days after it became known. A pool also ends when a newer pool takes over any of its
  members.
- Columns:
  - `pool_dist_up_atr`, `pool_dist_dn_atr`: distance to the nearest pool above / below.
  - `pool_touches_up`, `pool_touches_dn`: member count of those pools.
  - `touch_pool`: a pool lies in the ±0.25 ATR band around the candle.
  - `pool_swept`: the candle pierced a pool by at least 0.1 `atr_prev` and closed back inside.

**Anchored VWAP.**

- Anchors: the last same-tf fractal swing low and swing high with n = 5, confirmed by the end
  of candle i-1.
- VWAP: `sum(typical * volume) / sum(volume)` over the swing candle through i-1, with
  `typical = (h + l + c) / 3`.
- Empty when any of those candles is invalid or lacks volume. This includes DOGE/XRP daily
  volumes that overflow and are stored as MISSING.
- Intraday rows also get `vwap_day_dist_atr`: the UTC-day VWAP from the day start through
  i-1. It is empty on the day's first candle.

**Volume profile.** This needs 1m bars, so it is empty before the profile window is complete,
which first happens in 2024-02.

- Timing: at every UTC day open, the profile is built from the previous 30 / 90 complete days.
  It is used by every row whose candle opens on that day.
- Bins: `floor(ln(close) / 0.001)`, i.e. 10 bp.
- Approximation: each minute's whole volume goes to its close's bin, and the minute's range is
  ignored.
- Skipped minutes: those with a missing or compromised price, or missing volume.
- POC: the max-volume bin, reported at the bin centre. Ties go to the lower bin.
- Value area: start at the POC. While the area holds less than 70% of the volume, compare the
  two bins above with the two bins below (fewer at the profile's edge) and add the side with
  more volume. Ties go to the upper side. VAH and VAL are the area's upper and lower edges.
- Columns, for 30d and 90d: `dist_poc_*_atr`, `dist_vah_*_atr`, `dist_val_*_atr`,
  `in_value_area_*`, `touch_poc_*`.

Like location, sweep and htf, levels2 never changes `status`.

### Derivatives (deriv-v1) and flush (flush-v1)

Every value is the one known at the decision time. A missing input leaves the column empty; nothing
is filled from an older row.

**Funding.** All rows (intraday, daily, weekly) use the dk1 funding history from 2020, so the
90-day percentile has no hole at the start of the intraday table (2024-01). The rd funding is not
used for the columns; the manifest records `funding_parity` per symbol: on the overlap of the two
series, with settlements keyed by their time floored to the minute, the number compared, equal,
differing, rd-only and dk1-only. A settlement counts once its time, floored to the minute, is at or
before the decision.

- `fund_last`: the last settled rate.
- `fund_pct90`: the fraction of the rates settled in the previous 90 days (including the last one)
  that are <= `fund_last`. Empty when the 90-day window starts before the loaded funding.
- `fund_clamped`: `fund_last == 0.0001`, the clamp value most settlements sit at.
- `fund_cum_24h`: the sum of the last 3 settlements.

**Premium and basis** (only where 1m data exist, i.e. 2024+; empty for earlier daily/weekly rows):

- `prem_last`: the premium-index close at the candle's last minute.
- `prem_z`: the candle's mean premium (all its minutes present) against the mean and standard
  deviation of earlier same-tf candle means in the trailing 30 days; needs at least 100 values. In
  practice this is empty for daily and weekly rows.
- `basis_bp`: `1e4 * (mark close - index close) / index close` at the candle's last minute.

**Open interest and positioning** (only months published as mx releases; other rows are empty and
the manifest counts them per symbol and timeframe in `oi_missing_counts`: non-invalid rows whose
`oi_value_end` is empty, i.e. outside published months or in a metrics gap; invalid candles carry no
features and are not counted):

- A metrics row stamped `T` is usable from `T + 5 min` (`metrics_lake.usable_from_ms`). A value at
  time `t` is the latest 5-minute period usable at `t`; a NaN period stays empty.
- `oi_value_end` / `oi_value_start`: the open-interest value usable at the decision / at the
  candle's open. `doi_pct = end / start - 1` needs both.
- `doi_z`: the screen's robust z (median and 1.4826 × MAD) of `doi_pct` against earlier same-tf
  candles in a trailing window: 30 days with at least 500 / 300 / 100 values for 15m / 1h / 4h,
  365 days with at least 90 for 1d, 730 days with at least 52 for 1w.
- `oi_vol_ratio`: `oi_value_end` divided by the sum of close × base volume over the 24 hours
  ending at the decision (intraday: the row's own candles; daily/weekly: the dk1 day ending at the
  decision).
- `ls_global`, `ls_top_acct`, `ls_top_pos`, `taker_ls`: the global account, top-trader account,
  top-trader position and taker volume ratios usable at the decision.
- `ls_global_z`: the screen's global-ls z (30-day history, at least 500 values, hourly decisions)
  at the latest hourly decision at or before the row's decision.

**Flush proxy (flush-v1).** With `ret = ln(close / open)` of the candle:

- `flush_down`: `ret <= -3 sigma_tf` and `doi_z <= -3` (price fell and OI collapsed: a long
  liquidation proxy).
- `flush_up`: `ret >= +3 sigma_tf` and `doi_z <= -3` (a short squeeze proxy).
- `flush_dir`: -1 for flush_down, +1 for flush_up, 0 for neither.

Real liquidation prints join in a later PR (#349). Neither group changes `status`.

## Doji event study

A pre-registered screen on the candle table (`patterns/study_cells.py`, `patterns/pattern_study.py`,
`.github/workflows/pattern-study.yml`), in the style of `benchmark/screen.py`. It is descriptive
only: never a PASS, no trades, no labels, no ledger entry. The report states how many hypotheses
were examined. The cell definitions (`doji-cells-v1`, event `doji-geo-v2`) are frozen once results
exist; any change is a new version.

### Definitions

- **Event E (doji-geo-v2):** status ok, `body_ratio <= 0.10`, `range > 0`, `range_atr >= 0.5`.
- **Controls:** the same context, the same size class (`range_atr >= 0.5`) and another shape
  (`body_ratio > 0.10`).
- **Population:** segment rows with a valid one-candle outcome window (`fwd_ok_1`) whose candle is
  not invalid (`status != invalid`). Rows of status `insufficient` stay: groups that never change
  `status` (location, sweep, htf, levels2, derivatives, flush) must not drop rows. A cell's own
  inputs decide, through the missing-control rule below.
- **Direction:** `s` is fixed by the cell rule, never by the outcome.

| cell | context | direction |
|---|---|---|
| A | all candles | unsigned |
| C1 | `adx_prev >= 20`, `trend_dir != 0`, `|extension_prev_atr| < 1.5`, no level touch, nearest level > 0.5 ATR | `trend_dir` |
| C2 | `adx_prev >= 20`, `extension_prev_atr * trend_dir >= 2`, the trend-side extreme at or beyond the prior-20 extreme | `-trend_dir` |
| C3 | exactly one side swept (`sweep_both` false) | `sweep_dir` |
| C4 | any level touched | `-touch_approach_side` |
| C5 | `squeeze_on_prev` | unsigned |
| C6 | `adx_prev < 20`, `trend_dir != 0` (negative control, expected null) | `trend_dir` |
| C7 | `flush_dir != 0` | `-flush_dir` |
| C8 | relative volume >= 2 (intraday: `rel_volume_slot`; daily/weekly: `rel_volume_prior20`) and `|ret3_prev_sigma| >= 2·√3` | `-sign(ret3_prev_sigma)` |

C8: `ret3_prev_sigma` is a 3-candle return in **one-candle** sigma units, so a "2 sigma" 3-candle
shock is `|ret3_prev_sigma| >= 2·√3` (about 3.46).

The C2 prior-20 extreme is the sweep group's prior20 reference: the max high / min low of
candles i-20..i-1, all valid. It is rebuilt from consecutive table rows of the same symbol and
timeframe.

**Outcomes (m = 1).**

- Signed cells: `y = s * ret_1_sigma`. Unsigned cells: `y = ln_range1_sigma`.
- Hit = `s * ret_1_bp > 0`. Ties (`ret_1_bp == 0`) are excluded from hit rates and counted.

### Estimands

**Primary (K = 9: A, C1..C8).** Pooled over the six symbols and the intraday timeframes:

    y = alpha_{symbol,tf} + gamma_hour + beta * D + delta' X + e

- `D` is E within the cell's context.
- The sample is the context rows with `range_atr >= 0.5`.
- `X = [ln range_atr, ln rel_volume_prior20, atr_pct_prev, s * ret3_prev_sigma (signed cells),
  |ret3_prev_sigma|]`. Rows with a missing control are excluded and counted.
- Covariance is clustered by the UTC day of the candle open.
- p-values are normal approximations on the clustered t, Holm-adjusted over the 9 tests.
- The robustness estimator is a stratified difference in means on the same sample.
  - Strata: symbol, tf, 6-hour bucket, `atr_pct_prev` tercile, `range_atr` bin
    [0.5, 0.75, 1, 1.5, 2.5, inf) and `rel_volume_prior20` tercile (sample quantiles).
  - Weights are member counts; a stratum needs at least one member and one control.
  - The CI comes from a UTC-day cluster bootstrap.

**Reported per cell:**

- Counts: members, controls and days, plus exclusions (missing controls, missing outcome).
- Mean y for members and for controls.
- Beta, its clustered SE and 95% CI, in sigma and in bp (beta × the members' mean `sigma_tf`
  in bp).
- The stratified difference with its CI, and beside it the same estimator with the decision-time
  session as an extra stratum (`stratified_session`; a robustness view, not part of the Holm
  family).
- The hit-rate difference, with a Wilson interval for each rate.
- The Holm p-value.
- The minimum detectable effect at two multipliers: `2.8 × SE` (alpha 0.05, power 0.8) and the
  Holm-adjusted `(z(1 - 0.05/(2·9)) + z(0.8)) × SE`, about 3.6, computed from the normal quantile.
- The cost hurdle: an 11 bp round trip, i.e. `11 / mean sigma_tf bp` in sigma.
- The members' mean signed return (bp) and the controls' mean (bp), each with a UTC-day-clustered
  95% CI (gross, one candle).
- `members mean - hurdle` next to `beta - hurdle` (hurdle stays 11 bp).
- A flag when members < 100.

Unsigned cells have no bp, hit-rate or cost line.

**Population, base rates and diagnostics (counts and rates, no tests):**

- Population counts per timeframe: segment rows, invalid candles, rows without a valid forward
  window and the population (these counts also appear in the public log); per cell, the rows
  excluded for missing controls.
- Context base rates per cell and intraday timeframe: context rows, events and controls, and
  their shares of the population.
- Touch base rates per level set (and `any_touch`) and timeframe: rows evaluated (non-invalid rows
  whose touch flag is not empty) and the share with the flag set. For round levels the median
  round step (price units) over the median candle range, per symbol and as the median over
  symbols. A set whose touch share is above 90% or below 1% is labelled **degenerate: not
  interpretable**: it stays in the output and is never counted as a null result. Cells C1 and C4 use
  `any_touch`, so a degenerate `any_touch` makes them not interpretable at that timeframe. The
  `touch_round` column and its definition are not changed by the study.

**Descriptive (labelled, untested):**

- Per-timeframe and per-symbol tables.
- m = 2 and 4 (`s * ret_m_sigma` and the side's MFE / MAE).
- Sensitivity runs with `body_ratio <= 0.05` and with `range_atr >= 1.0`.
- A subtype split.
- Dose-response within E for cell A: quintiles of `body_ratio`, `range_atr`,
  `rel_volume_prior20` and `nearest_level_dist_atr`, with Spearman(quintile, mean) and a
  day-bootstrap CI.
- Sessions (exploratory): per cell × intraday timeframe × session, with the session taken from the
  hour of the decision time (the candle's `end_ms`, UTC): asia 00-08, europe 08-13, us 13-21,
  late 21-24 (daily and weekly rows have the session "all"). Each session reports the event and
  control counts, the members' and controls' mean signed return (bp; unsigned cells report the
  next-candle ln range in sigma) with a day-clustered CI, their difference and the Wilson hit
  rates. A session with fewer than 30 events is shown as "too few", not as zero. Session results
  are exploratory: a pattern present in only one session is reported as session-specific, and any
  follow-up tested on validation must be pre-registered in a plan file first. Sessions add no
  hypothesis (K stays 9).
- Sigma-seasonality diagnostic: on the control rows of cell A only (population, `range_atr >= 0.5`,
  `body_ratio > 0.10`), per intraday timeframe and UTC hour of the decision time, the mean of
  `|ret_1_sigma|` (the next candle's absolute return over `sigma_tf`), the count, and the ratio of
  the largest to the smallest hourly mean. It would be roughly flat if sigma were well scaled.
  Sigma (`ewma-3d-5m-v1`) is not changed; this only reports.
- Daily and weekly families run separately per timeframe, pooled over symbols, with symbol fixed
  effects and no hour effects. A cell is "not tested" when it has fewer than 30 members. A
  development run uses the `development-ext` daily rows.

### Runs and output

- **Inputs:** `segment` (development | validation; hidden is refused by the guard) and
  `table_report` (a candle-table report hash or path in the private repo).
- **Replication:** an optional `reference_report` (a development report) adds a same-sign
  replication column to a validation run.
- **Output:** `reports/pattern-study/doji-<segment>__<hash16>/` with `report.md`, `cells.csv` and
  `summary.json`. The hash leaves out the commit and creation time, so a run with identical inputs
  finds its directory and skips.
- **Public log:** counts, the number of hypotheses examined and the private path; never an effect
  size.

### What a result means

**What it can show.** A cell with a Holm-significant beta says this: in that context, doji
candles are followed by a different next-candle outcome than same-size non-doji candles, after the
listed controls.

**What it cannot show.**

- It is not a profitability verdict. That needs stop-aware, cost-aware trade results
  (CLAUDE.md section 2). The report's bp lines are gross, one candle, no stop: the members'
  mean is compared with the 11 bp round trip only as a hurdle, not as a result after costs.
- It does not prove the doji itself is causal, because the controls are observational.
- A development result must replicate with the same sign on validation before anything is built
  on it.
- An effect below the hurdle (`beta - hurdle < 0`) is not tradable on its own, even if it is
  significant. A members' mean below the hurdle (`members mean - hurdle < 0`) says the doji
  candles' own gross drift in that context does not cover the cost either; both can differ, so
  both are shown.
- A degenerate level set is not a null result: it means the set cannot discriminate at that
  timeframe.
- Session and hourly tables are exploratory; they cannot rescue or reject a primary cell.

A null result is informative only when its MDE is well below the hurdle: compare the MDE at 2.8 SE
and at the Holm alpha with the hurdle. If the Holm MDE exceeds the hurdle, the study could not
have detected a tradable effect in that cell.

## Doji exploration (doji-explore-v1)

Free, descriptive discovery after the doji study found no standalone directional edge (owner
decision 2026-10-15). `python/market_analysis/patterns/pattern_explore.py`,
`scripts/research/pattern_explore.py`, `.github/workflows/pattern-explore.yml`. **No stops in
discovery and no required outcome**: the tool describes what follows a doji; strategies are built
later from what it finds. The doji study and its cells are unchanged.

**Hygiene.** Development only (intraday `development`, daily/weekly `development-ext`). Validation
needs the plan id of a plan committed as `plans/doji-explore__<id16>.plan.json` (verified with
`read_plan`); the hidden stretch is always refused by `check_segment`. Paths never cross the
segment boundary. Every slice x metric family evaluated is appended to
`experiments/doji-explore.jsonl` (one line each: timeframe, slice, bucket, family, horizons, code
version, n, report hash, run id), and a rerun of the same table and code is a REPLAY, not a new
variant. BH q-values are over every test of the run, and the count is in the report.

**Per timeframe.** Every analysis runs per timeframe; nothing is pooled across timeframes (pooling
let 15m dominate the doji study). Tiers: `intraday` (15/60/240) or `daily_weekly` (1440/10080).

**Groups.**

- Event E = doji-geo-v2 (`body_ratio <= 0.10`, `range_atr >= 0.5`); descriptive variants
  `body_ratio <= 0.05` and `range_atr >= 1.0` (direction-free headline metrics only).
- Matched controls: `range_atr >= 0.5`, `body_ratio > 0.10`, reweighted to the events over strata
  symbol x tf x 6-hour bucket x `atr_pct_prev` tercile x `range_atr` bin x `rel_volume_prior20`
  tercile (a control's weight = events / controls of its stratum). Events in strata without
  controls are counted (`events_unmatched`) and left out of the differences. Intraday runs also
  report the strata with the decision-time session added.
- Random baseline: population rows sampled without replacement (seed 20261017) per symbol x UTC
  hour of the open, 2 x the events of that cell (all rows when fewer), so the hour mix matches.

**Paths.** For row i, the consecutive table rows i+1..i+N (N = 24 intraday, 20 daily, 13 weekly;
built to N + 5 so the break rule keeps N candles after a break). A step must exist, open exactly k
candles later, not be invalid and lie in the same segment; the path stops at the first failure.
Incomplete paths are counted per horizon and excluded there, never filled. Entry = open of i+1.
Units: one-candle sigma of row i (`sigma_tf`; multi-candle values are NOT divided by sqrt(h)), ATR
(`atr_prev`, price distance / ATR) and bp; `sigma_seas_factor` gives an h = 1 seasonal drift where
the table has it.

**Direction-free (per h in 1, 2, 3, 4, 6, 8, 12, 24; daily 1..12, 20; weekly 1..8, 13).** Up and
down excursion (max high / min low vs the entry), total range, net drift, and for +/-k ATR with k
in 0.5, 1, 1.5, 2, 3: up-first / down-first / AMBIGUOUS (both inside one candle) / neither within
h, the resolution share (a barrier reached within h) and the time to the first touch.

**Directional rules (no stop).** (a) Break: the first close beyond the event candle's high (long)
or low (short) within K in 1, 2, 3, 5 candles (K = 3 primary); entry at that close; returns h
candles after the break with the side's MFE/MAE; the fade is the negative and is reported too.
(b) Subtype: dragonfly long, gravestone short, entry at the next open; controls use the same wick
rule without the body rule (lower >= 0.6 and upper <= 0.1 long, the mirror short); baseline rows
get a seeded random side with the events' long share. (c) The break split into with-trend /
counter-trend / no trend under `trend_dir`, `trend_agree` (trend_dir where it agrees, else no
trend), `swing_trend_dir` and `htf_trend_dir`. Each reports n, mean, median, 10/25/50/75/90
quantiles, share > 0, MFE/MAE, bp, a day-clustered SE and a day-cluster bootstrap CI, for events,
matched controls and baseline, plus event - control and event - baseline with their CIs.

**Excursion map (a map, not a recommendation).** For rules (a) K = 3 and (b): stop in 1, 1.5, 2, 3
ATR x target in 1, 2, 3, 4 ATR within N candles (else a time exit at the close), and stop-only time
exits at every h. First passage over candle highs/lows after the entry; a candle opening beyond
the stop exits at its open. Both barriers inside one candle = AMBIGUOUS: reported as a share,
excluded from one mean and counted as a stop in the other (never resolved optimistically). Gross
and net of `cost_bp` (default 11) per cell, with n.

**Slices.** A registry (`SLICES`, each with the columns it reads; a slice whose columns are absent
from the table is skipped and listed, so the prev/ctx2/lv3 slices of #358 apply once the table has
those groups): all, symbol, session, relative-volume quintile (slot volume
intraday when present), range_atr, body_ratio, subtype, adx, extension, each trend definition, each
touch set separately (all loc-v1/lv2/lv3 sets and `touch_sig`), significant-level distance, six
regime labels, `doi_z`, `fund_pct90`, `prem_z`, prev-doji x `confirm_dir`, `climax_age6`,
`climax_vol_max6`, `flush_prev_dir`, and volume x `touch_sig`, volume x `trend_agree`, subtype x
`touch_sig`. Controls are matched again inside each bucket (body and subtype buckets, which define
the event, compare with all matched controls). Families tested per bucket at two lead horizons
(4 and 12 candles intraday, 3 and 12 daily, 2 and 8 weekly): `expansion` (range in ATR),
`resolution` (+/-1 ATR within h), `drift` (sigma), `break_follow` (rule (a) K = 3, sigma) and
`subtype_dir` (rule (b), sigma); events vs matched controls with a day-clustered t.

**Leads.** |t| >= 2, n >= 100 events, the same sign in both halves of the segment (split at the
middle of its time range) and in at least 4 of the 6 symbols. Output is never gated on
significance; `leads.csv` lists the top 30 leads per family, and every test is in `slices.csv` with
raw p and BH q. A lead is a candidate for a validation plan, not a result.

**Review packs.** `review/review_<tf>_all.html` (and an optional `--review-slice slice=bucket`
pack): seeded random events (any outcome), seeded random controls, and a balanced pack of random
hits and misses of rule (a) K = 3 at the first lead horizon. Every chart states its sampling rule.
A balanced pack with no hits (or no misses) is withheld: no pack contains only misses. (The repo
has no earlier doji chart generator; `doji_report.sample_events` is the time-spread sampler of the
frozen doji-v1 report and is not changed.)

**Output.** `reports/pattern-explore/doji-<segment>__<hash16>/`: `summary.json`,
`direction_free.csv`, `directional.csv`, `excursion_map.csv`, `slices.csv`, `leads.csv`, `report.md`
and `review/*.html`. The public log carries counts, the number of tests, the private path and the
hash only.

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
