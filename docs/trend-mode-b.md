# Multi-day trend, Mode B portfolio evaluation (trend-v1)

> **Provisional.** This document is AI-generated and is not the source of truth. The owner's
> intent in [`CLAUDE.md`](../CLAUDE.md) takes precedence. Ticket:
> [#222](https://github.com/ibrahimjaved12/crypto-watcher/issues/222). Spec: SPEC-2 section 1
> ([`strategy-specs/SPEC-2-trend-ta-vol.md`](strategy-specs/SPEC-2-trend-ta-vol.md)).

Code: `python/market_analysis/benchmark/trend.py`. Run: **Trend run** workflow
(`.github/workflows/trend-run.yml`, script `scripts/research/trend_run.py`) on the development
or validation segment. The hidden segment is refused before anything is downloaded.

## What is evaluated

Mode B treats each variant as a daily **portfolio return stream**: one number per UTC day, the
net return as a fraction of equity. Mode A (trade level) is a separate later step.

### Data and timing

- **Inputs:** dk1 daily bars and funding, 2020-01 to the segment's last month, for the six
  benchmark symbols. The years before 2024 are indicator warm-up only. The weight path runs
  through the warm-up, but only segment days are evaluated.
- **Information set:** a position held on day t uses closes up to day t-1 only. The position
  is set at the day-t open and held to the day-t close.
- **Undefined inputs:** a component or signal is undefined while any close in its lookback
  window is missing, or while the window is not yet full. An undefined weight is 0 (flat).
  An ensemble component restarts from state 0 after an undefined day.
- **Missing days:** a day whose own open, close or funding is missing is not evaluated for
  that symbol. It never changes the weight path, so a weight never depends on its own day or
  any later day.

### Donchian ensemble (family `ens`)

For each lookback L in 5, 10, 20, 30, 60, 90, 150, 250 and 360 days, every close d gets a
channel from the L closes before it:

- `upper = max(close[d-L..d-1])`
- `lower = min(close[d-L..d-1])`
- `mid = (upper + lower) / 2`

The channel is compared with `close[d]`, and the result sets the position of day d+1. This
is the SPEC-2 formula with the decision moved one day, so day t's information set is closes
through t-1. A channel that included the compared close could never be broken.

The component state is updated in this order:

1. `close > upper` gives +1.
2. Otherwise `close < lower` gives -1. A long can flip straight to short.
3. Otherwise, a long exits to 0 when `close < mid`. The exit trails the midline.
4. Otherwise, a short exits to 0 when `close > mid`.
5. Otherwise the state is kept.

The signal S is the mean of the nine states. It is defined only when all nine are warm.

### Time-series momentum (family `tsmom`)

`s = sign(ln close[t-1] - ln close[t-1-L])`, with L = 7, 14 or 28 days.

### Sizing, band and costs

- **Weight:** `w = clip(S * sigma_target / sigma_hat, -2, +2)`.
  - `ens` uses `sigma_hat` = the sample standard deviation (ddof 1) of the last 90 daily log
    returns, times sqrt(365).
  - `tsmom` uses a zero-mean EWMA standard deviation (half-life 60 days, normalized weights)
    times sqrt(365). It is defined after at least 120 returns since the last gap; this
    warm-up is our choice, because an EWMA has no fixed window to fill.
- **No-trade band:** the previous weight is kept unless one of these holds:
  `|w - w_prev| >= 0.10 * |w_prev|`, the sign changes, or `w_prev == 0` and `w != 0`.
- **Day return:** `ret = close / open - 1`.
- **Cost:** 11 bp per unit of turnover, `|w_t - w_{t-1}|`, charged at the open. Every variant
  is also evaluated at 2x cost.
- **Funding:** `- w_t * sum(rate)` over the settlements with `open_t < calc_time <= open_t + 1
  day`. A long pays when the rate is positive. A settlement exactly at the next 00:00 is paid
  by the position held into it. The segment's very last settlement falls in the next month,
  which is never loaded, so it is missing.
- **Day net:** `w * ret - cost - funding`.
- **Portfolio:** each day, the equal-weight mean over the symbols defined that day. A symbol
  counts when its weight is defined (or it is closing a non-zero weight) and the day can be
  evaluated. Each symbol is sized to sigma_target on its own. Per-symbol streams are kept as
  diagnostics.
- **No catastrophe stop in v1:** daily bars cannot place one honestly. It belongs to Mode A.

## The K = 9 variants (fixed, no lookback selection)

| variant | definition |
| --- | --- |
| `ens_ls_25` | ensemble, long/short, sigma_target 0.25 |
| `ens_lo_25` | ensemble, long-only (S = max(S, 0)), 0.25 |
| `ens_ls_15` | ensemble, long/short, 0.15 |
| `ens_ls_25_sub3` | ensemble on lookbacks 20, 60 and 150 only, 0.25 |
| `tsmom_7`, `tsmom_14`, `tsmom_28` | TSMOM, 0.25 |
| `ens_ls_25_mfilter` | price-to-MA score M = number of k in (20, 50, 100, 200) with close[t-1] > SMA_k. The long part of S is zeroed when M < 3, the short part when M > 1. |
| `ens_ls_25_rvfilter` | new positions (w_prev = 0, w != 0) are blocked while the 30-day realized-vol percentile within the trailing 365 days is >= 70. Existing positions follow the normal rule. |

## Controls

Each variant gets four controls:

1. **Volatility-targeted buy-and-hold:** S = 1, with the variant's own sizing, band and costs,
   and no filter. The variant's alpha against it comes from an OLS regression of its daily
   net on the buy-and-hold daily net, with Newey-West standard errors (Bartlett, lag 10).
2. **Circular time-shift placebo:**
   - The whole segment signal series of every symbol is shifted by the same k days.
   - k is drawn from `rng.u64_words` in [30, T - 30]; there are 1000 shifts, the same for
     every variant.
   - Weights are recomputed with the real sigma and band, starting from the real weight
     carried into the segment.
   - p = share of shifts whose total (equivalently mean) daily net is >= the real one.
3. **Long and short legs:** streams built from the positive and negative parts of w, each
   with its own turnover cost and funding.
4. **Without the best calendar month:** the mean daily net after removing the month with the
   largest total.

## Evaluation

All of this reuses the #182 harness:

- **Units:** daily net returns become integer parts per million of equity,
  `round(x * 1e6)`, half to even.
- **Per variant:**
  - **Power first:** mu_min from `power.min_detectable_edge_per_day(T, sigma_daily)` is
    recorded before any verdict.
  - **Interval:** `evaluate.bootstrap_ci(B = 2000)`. Its `mean_daily_r` is the mean daily
    return fraction.
  - **Diagnostics:** annualized Sharpe, `evaluate.drawdown`, turnover per year, funding drag.
- **Across the nine:**
  - The (T, 9) integer matrix goes through `spa.spa_test`, `stepm.stepm`, `stepm_p_values`
    and `best_trial.best_trial_dsr`.
  - The threshold is `runner.required_t(n_trials)`. n_trials counts the whole question:
    the ledger history plus this run.

### Verdict (trend-specific)

The core conditions are:

- StepM rejects.
- The bootstrap CI lower bound is > 0.
- The bootstrap t is >= required_t.
- The placebo p is <= 0.05.
- The alpha-vs-buy-and-hold t is >= 2.

The verdicts are:

- **PASS:** the core conditions hold, and the mean is positive at 1x cost, at 2x cost and
  without the best month.
- **FRAGILE:** the core conditions hold and the 1x mean is positive. Only the 2x cost or the
  removal of the best month fails.
- **FAIL:** anything else.

## Ledger and outputs

- **Ledger:** one `experiment_log.TrialRecord` per variant and segment:
  - `question_id trend-v1`, `family_id trend`, `strategy_version trend-v1`
  - `strategy_id` = the variant name, config = all parameters plus the symbol universe
  - `counts_toward_n=True`, `count_reason "variant evaluated"`
  - Records are appended to `experiments/trend-v1.jsonl` in the private repo. Re-running an
    identical variant appends a REPLAY that does not count.
- **Report:** `reports/trend/<segment>__<hash16>.json` and `.md` in the private repo, in one
  commit with the ledger.
- **Public log:** per variant, the name, segment, T days and number of defined symbols, plus
  the report hash. No returns, Sharpe, t or verdicts.

## Not in v1

- **Not built yet:**
  - Mode A (trade-level labels and a catastrophe stop).
  - The 4h-bar variants and the 4h Donchian breakout of SPEC-2 1.3.
  - The funding filter (H11) and the season filter from #223.
- **Hidden segment:** not run until a plan exists.
