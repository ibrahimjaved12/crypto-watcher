# Objective regime labels and calendar hypotheses (regime-v1)

> **Provisional.** This document is AI-generated and is not the source of truth. The owner's
> intent in [`CLAUDE.md`](../CLAUDE.md) takes precedence. Ticket:
> [#223](https://github.com/ibrahimjaved12/crypto-watcher/issues/223). Spec: SPEC-2 sections
> 4-5 ([`strategy-specs/SPEC-2-trend-ta-vol.md`](strategy-specs/SPEC-2-trend-ta-vol.md)).

Code: `python/market_analysis/benchmark/regime.py`. Run: **Regime build** workflow
(`.github/workflows/regime-build.yml`, script `scripts/research/regime_build.py`).

**Descriptive only.** Nothing here is a strategy. Nothing counts as a PASS, and nothing enters
the experiment ledger. The role test (whether a label improves the reference strategies)
needs the trend results of #222 and comes later.

## The table

`regime-v1` has one row per UTC day and symbol, from 2020-01-01 through 2025-12-31:

- **Point in time:** each row is computed at 00:00 UTC of day t from closes through day t-1.
- **No tuning:** no smoothing, and the thresholds are frozen.
- **Undefined values:** during warm-up or after a gap a value is an empty cell, never filled.
- **Data:** dk1 daily closes and BTCUSDT funding.
- **Hidden stretch:** never read. The loaders stop before 2026 rows, and the guard refuses
  later months without a token.

### Market labels (BTCUSDT)

| column | definition |
| --- | --- |
| `mkt_t1` | +1 if close > SMA200, else -1 |
| `mkt_t2` | +1 if SMA50 > SMA200, else -1 |
| `mkt_dd`, `mkt_dd_state` | close / running maximum of closes - 1. States: bull > -20%, neutral -20% to -50% (both ends inclusive), bear < -50%. |
| `mkt_mm`, `mkt_mm_flag` | Mayer multiple close / SMA200. Flags: overheated > 2.4, capitulation < 0.8, else normal. |
| `mkt_rv30`, `mkt_rv_pct`, `mkt_rv_state` | std (ddof 1) of the last 30 daily log returns * sqrt(365). The percentile is its rank within the trailing 365 days including today: 100 * share of values <= today. States: calm < 30, stressed > 70, else normal. |
| `mkt_fr30`, `mkt_fr_state` | mean BTCUSDT funding rate per 8h over the settlements in [t - 30 days, t). Each rate is normalized as rate * 8 / interval_hours, and the mean is exact. States: crowded_long > 0.0001, crowded_short < 0, else neutral. |
| `halving_months`, `halving_bin` | whole months since the latest halving on or before the day (2012-11-28, 2016-07-09, 2020-05-11, 2024-04-20). Bins: 0-6, 6-18, 18-30, 30-48, 48+, each with a lower bound inclusive. |
| `mkt_season` | Bull if t1 = +1, t2 = +1 and dd > -20%. Bear if t1 = -1 and t2 = -1. Otherwise Transition. |
| `mkt_season_h` | Hysteresis. The label changes only after a new candidate label has held for 3 consecutive days. The first defined day starts the held label, and an empty day restarts it. |

**Caveat:** the running maximum only covers the futures data from 2020-01. Before the 2021
highs, BTC drawdowns are therefore measured against the dataset's own maximum, not the 2017
spot high. The evaluation years 2024-2025 are not affected.

### Per-symbol labels

These come from each symbol's own closes:

- **Same rules as the market:** `sym_t1`, `sym_t2`, `sym_dd`, `sym_dd_state` and `sym_season`.
- **`sym_er20`:** Kaufman efficiency ratio,
  `|close[t-1] - close[t-21]| / sum of |daily changes|` over the same 20 days. It is 1 on a
  straight line.
- **`sym_vr5`:** variance ratio `Var(r5) / (5 Var(r1))` over the trailing 180 daily log
  returns. It uses overlapping 5-day sums and ddof 1, and is near 1 on iid returns.
- **`sym_hurst`:** `0.5 + ln(vr5) / (2 ln 5)`.

### Diagnostics

For every label there are two scopes: all table days, and development + validation days only.
Each scope reports:

- defined days and days per state
- runs
- mean state duration: defined days / runs, with runs at the edges included
- flips: changes between consecutive defined days
- flip rate: flips / consecutive defined pairs * 365

## Calendar hypotheses (exactly four, pre-registered)

These use development + validation days only, 2024-01-01..2025-12-31. They are never a signal
on their own.

| id | test | data |
| --- | --- | --- |
| H1 | October vs other months: mean monthly log return difference | dk1 BTCUSDT month-end closes |
| H2 | months 6-18 after a halving vs others: mean daily log return difference | dk1 BTCUSDT daily log returns |
| H3 | long 21:00-24:00 UTC (21:00 open to 00:00 open) vs the other seven 3-hour windows, gross | rd 1-minute BTCUSDT opens |
| H4 | weekend (Sat, Sun UTC) vs weekday daily log returns | dk1 BTCUSDT daily log returns |

For H3, the report also prints the gross mean of the 21:00 window net of the taker round
trip (12 bp, from `costs.COST_MODEL_V1`) and of the maker round trip (4 bp: maker entry and
exit), side by side. A day needs all eight windows. The last loaded day has no next-day 00:00
open, so it is dropped.

### Inference

**Calendar-month block bootstrap:**

- Whole months are resampled with replacement, B = 2000, drawn with `rng.u64_words`.
- The statistic is mean(group) - mean(rest).
- A resample missing either group is undefined, and those resamples are counted.
- The report gives n, the difference, the nearest-rank 95% interval, the bootstrap standard
  error and t.

**The "cannot pass" line** is computed from the number of months:

- **Few group months (H1, H2):** suppose the tested group covers k of n months. If C(n, k)
  month relabellings cannot reach the two-sided p of t = 3.3 (about 0.00097, which needs at
  least 1035 relabellings), the line says so. For H1, 24 months with 2 Octobers give
  C(24,2) = 276.
- **Otherwise:** when |t| < 3.3, the line states the difference that 3.3 bootstrap standard
  errors would require.
- **When |t| >= 3.3:** it still says "descriptive only".

## Outputs

These go to `reports/regime/` in the private repo, in one commit:

- `regime-v1__<hash16>.csv.gz`: fixed columns, rows in date-then-symbol order, a
  deterministic gzip.
- `regime-v1__<hash16>.json`: the manifest. It holds the columns, row count, table sha256,
  thresholds, diagnostics, the calendar results, inputs, and the report hash.
- `regime-v1__<hash16>.md`: a readable summary.

The public log shows only the rows written, the number of hypotheses and the report hash.
