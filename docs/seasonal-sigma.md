# Seasonal sigma and labels-v2 (lb2)

> **Provisional.** This document is AI-generated and is not the source of truth. The owner's
> intent in [`CLAUDE.md`](../CLAUDE.md) takes precedence. Ticket:
> [#220](https://github.com/ibrahimjaved12/crypto-watcher/issues/220).

## Why

The development calibration audit (label r3) found a problem with the plain EWMA sigma:

- The overall sd(z) passes.
- By UTC hour, sd(z) ranges from about 0.67 (03-06h) to 1.5 (13-15h).
- The 240 m barrier check fails for every symbol and half-life. The expiry share is about
  0.14 against 0.03 in theory, and the implied sigma ratio is about 0.74.
- mean|z| is about 0.67 against 0.80 for a normal. z is heavy-tailed, and its typical size is
  smaller than the EWMA says.

A single EWMA sigma therefore mis-sizes `k * sigma` stops. They are too wide in quiet hours,
too tight at the US open, and too wide in the typical state.

Nothing here is tuned to returns. The only criterion is the calibration audit.

## Intraday factors (`volatility.seasonal_factors`)

All values are integers. A factor of 1.0 is `FACTOR_SCALE = 10^6`.

- **Slots:** there are 48 UTC half-hour slots of six 5-minute blocks each.
- **Block returns:** `r2` is the same integer as in `build_variance`. Compromised and missing
  blocks have no return; they are skipped, never filled.
- **Point in time:** for UTC day D, the factors use only the 28 calendar days before D that
  have at least 90% of their 288 blocks with a return.
  - In each such day, `u = r2 / mean(r2 of that day)`.
  - A slot's value is the mean of u over its blocks and days.
  - The 48 values are normalised to average exactly 1: an integer largest-remainder split, so
    a day's factors sum to exactly `48 * FACTOR_SCALE`.
- **Warm-up:** there is no factor (MISSING) until 14 qualifying days exist.

## Variance and horizon sigma

- **`build_variance_deseasonalised`:** the same EWMA as `build_variance`, fed
  `r2 / f[block's day][slot]`, so the level is not polluted by the intraday cycle.
  - A block whose day has no factors does not update.
  - Warm-up, publication and the int64 guards are unchanged.
- **`horizon_sigma_seasonal`:** `var * sum of f[slot]` over the horizon's blocks.
  - It uses the entry day's factors, even for blocks past midnight, because those are the
    factors known at entry.
  - It returns the same scaled integer as `horizon_sigma`, and equals it when every factor
    is 1.
- `build_variance` and `horizon_sigma` are untouched.

## labels-v2

`LabelParams.sigma_model` selects the sigma model:

| | `ewma` (default) | `ewma-seasonal` |
| --- | --- | --- |
| Schema | `labels-v1` | `labels-v2` |
| Release prefix | `lb1-SYMBOL-FIRST_LAST-rN` | `lb2-SYMBOL-FIRST_LAST-rN` |
| Sigma | plain EWMA | seasonal model |
| Sigma column | `sigma` | `sigma_ewma_seasonal` (the header names the model) |
| Missing factors | n/a | a decision whose entry day has no factors is status V |

- **lb1 is unchanged:** with `ewma`, the params identity and the label bytes are exactly as
  before.
- **The v2 record** adds `sigma_model` and `seasonal_window_days`.
- **Build:** the **Label build** workflow takes a `sigma_model` input. Existing lb1 releases are
  never touched.

## Calibration audit

Each (horizon, half-life) row is reported per sigma model:

- `ewma` rows sit under `horizons`.
- `ewma-seasonal` rows sit under `horizons_seasonal`.

The workflow inputs are `sigma_models` (default both) and `seasonal_label_revision`.

Every z block, overall and by hour, also reports two robust statistics:

- `robust_sd = 1.4826 * MAD(z)`
- `mean_abs_ratio = mean|z| / 0.797885`

The verdicts are:

- **`robust_ok`:** both robust statistics are in [0.9, 1.1] overall and in [0.8, 1.2] in
  every UTC hour.
- **PASS** = `sd_ok` and `robust_ok` and, on 240 m rows, `barrier_ok`.
- **Barrier check:** it reads labels built with the same model, lb1 for `ewma` and lb2 for
  `ewma-seasonal`. When a symbol's release is missing it is NA, and only the sigma/z audit
  runs.
