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
- **PASS** (calibration-v2, P16) = (`sd_ok` or `robust_ok`) and, on 240 m rows, `barrier_ok`.
  This is looser than the pre-P16 rule (either statistic suffices): read a PASS with that in mind.
  The pre-P16 rule (`sd_ok` and `robust_ok` and the label-step barrier check) stays in the JSON as
  `pass_v1` / `barrier_ok_v1`.
- **Barrier check (P16):** `scan.label_trade` monitors every 1-minute high/low, so the
  discrete-monitoring widening uses the 1-minute monitoring step, `0.5826 sqrt(1/240)` = 0.0376
  sigma_h at 240 m (the old 0.145650 = `0.5826 sqrt(15/240)` used the 15-minute LABEL step by
  mistake; it is kept only as a traced field). Pre-stated criteria, identical for every model:
  (a) T/(T+S) within 0.02 of b/(a+b) for every (k, rr, side); (b) at k = 1 the sigma ratio implied
  by the pooled expiry share against the monitoring-step theory in [0.85, 1.15] for every rr;
  (c) at k = 2 the implied ratio is reported as DESCRIPTIVE (far barriers are hit more often than
  Brownian motion predicts: heavy tails and volatility clustering, which no scalar sigma can fix).
  `barrier_ok` = (a) and (b).
- **Barrier check:** it reads labels built with the same model, lb1 for `ewma` and lb2 for
  `ewma-seasonal`. When a symbol's release is missing it is NA, and only the sigma/z audit
  runs.

## Robust and horizon-calibrated sigma (labels-v3, P12)

The development audit (label r3 / lb2 r1) shows the seasonal model fixed time of day. z sd by
UTC hour is now about 0.86-1.05. Every row still fails the robust check, though:

- At 60 m and 240 m, robust sd is about 0.65-0.68 and mean|z|/0.7979 about 0.83-0.84, while sd
  is about 0.98.
- The 240 m barrier check still fails.

z is heavy-tailed, so a squared-return EWMA overstates the typical scale. Two models are added in
`volatility.py` and `benchmark/robust_sigma.py`. Neither has a variance-ratio term.

### `ewma-robust`

- **Scale:** an EWMA of |r5| instead of r5²: same half-lives, warm-up, integer fixed point
  (`ABS_SCALE = 10^10`) and int64 guards.
- **Deseasonalisation:** by slot factors computed on |r5| (`seasonal_factors_abs`), with the same
  28-day point-in-time recipe, normalised to mean 1.
- **Block scale:** `s = EWMA(|r| / g_slot) / 0.797885`.
- **Horizon variance:** `sum over the horizon's 5-minute blocks of (s * g_slot)^2`, i.e.
  independent increments.

### `ewma-robust-hcal`

`ewma-robust` times a horizon multiplier `c_h`, estimated point in time and expanding:

- **Definition:** at entry t, `c_h` is the median of `|ln(open[e+h]/open[e])| / sigma_robust_h(e)`
  over every past window on the label-step grid whose exit is at or before t, divided by
  0.674490.
- **Clipping:** `c_h` is clipped to [0.5, 2].
- **Availability:** it is undefined (V / no_sigma) until the first counted window is 60 days old.
- **Cost:** a streaming median with two heaps keeps it near-linear.

### Labels and audit

- **Labels:** both models produce schema `labels-v3`, under `lb3-` (ewma-robust) and `lb3h-`
  (ewma-robust-hcal). Two models cannot share one release identity, so the hcal model gets its own
  prefix. The sigma column is `sigma_ewma_robust` / `sigma_ewma_robust_hcal`. lb1 and lb2 are
  byte-identical (pinned in tests).
- **Audit:** reports all four models. Each model's barrier block adds the median, min and max
  implied sigma ratio (monitoring-step theory, both sides; the label-step value is traced), so the
  residual can be read. P16 adopted `ewma-robust-hcal` as the forward harness sigma model.
