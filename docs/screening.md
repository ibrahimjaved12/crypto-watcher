# Screening mode

> **Provisional.** This document is AI-generated and is not the source of truth. The owner's
> intent in [`CLAUDE.md`](../CLAUDE.md) takes precedence. Ticket:
> [#220](https://github.com/ibrahimjaved12/crypto-watcher/issues/220) slice B (SPEC-1 section 3).

## What a screen is and is not

A screen is a cheap first look at a signal's *state*. A benchmark question uses only the
triggered trades; a screen uses every decision time. It answers two of the owner's
questions:

- Does a stronger signal do better?
- Is there anything left after a stop-free time exit?

A screen is descriptive. It creates no trades, labels or experiment-ledger entries, and it
**never counts as a PASS**. Anything it finds still has to pass the benchmark before it can
mean anything.

The **Screen** workflow (`.github/workflows/screen.yml`, script `scripts/research/screen.py`,
library `python/market_analysis/benchmark/screen.py`) runs on the development or
validation segment. The hidden guard refuses the hidden segment. The full report goes to
`reports/screens/` in the private research-data repo. The public log shows only symbol,
horizon, segment, observation counts and exclusion counts.

## First series: `of-cum240-z`

The only series so far is the taker-imbalance robust z of the of-v1 `cum240` rule, at
hourly decisions with history from 2024-01 (`order_flow.cum240_z_series`). The rule's
trading signals come from this same series, so the screen sees exactly what the rule sees.

## Definitions

- **Entry and exit:** a decision is made at the end of minute d. The entry is the open of
  minute e = d + 1, and the exit is the open of minute e + h, for h = 1, 2, 4, 8, 16 and
  24 hours.
- **Forward return:** r = ln(open[e + h] / open[e]), in basis points.
- **Exclusions:** a window that contains a missing or compromised minute is excluded and
  counted. So is a window whose exit falls at or after the segment end. The two counts are
  reported separately.
- **Controls:** all are known at the decision minute.
  - past240: the log return of the last 240 minutes, in bp.
  - sigma240: the 3-day EWMA volatility for a 240-minute horizon, from the label engine's
    volatility code.
  - The UTC hour of the decision.

## Regression and IC

For each horizon, one ordinary least-squares regression is pooled over the six symbols:

`r = alpha_symbol + gamma_hour + beta * z + delta * past240 + eta * sigma240`

It also runs without controls (`r = alpha_symbol + beta * z`). Comparing the two betas
shows whether the flow signal is just momentum in disguise.

Standard errors are clustered so that overlapping windows are not counted as independent.
Up to 4 hours the clusters are UTC days. For longer horizons they are blocks of
ceil(h / 1440) + 1 days. The naive standard error is shown next to the clustered one.

The IC is the Spearman rank correlation between z and the symbol-demeaned return.

## Bucket table and dose-response

The bucket table uses **every hourly decision with |z| >= 2**, not only the moments when
z crosses a threshold. This is the state version: it asks whether being in a strong state
pays, independent of the crossing and cool-down machinery. Buckets are |z| in [2, 2.5),
[2.5, 3), [3, 3.5) and [3.5, inf), each split by the sign of z, where the side is the
sign of z. Each cell reports:

- N and the number of clusters;
- gross bp (side x r);
- drift-adjusted bp, which subtracts the symbol's mean return over all decision times;
- the round-trip cost: the taker fee plus the market-slippage floor of the benchmark cost
  model, on entry and exit (12 bp; no maker orders, no stop);
- funding bp, using the price in place of the mark price;
- net bp (gross minus cost minus funding).

Standard errors come from a 2000-draw cluster bootstrap with a fixed seed.

**Dose-response** is one pre-registered hypothesis per horizon. Both signs are pooled, and
two numbers are shown side by side:

- the weighted least-squares slope of the drift-adjusted bucket means on the bucket index
  (0..3), weighted by N;
- the Spearman correlation between the bucket index and the bucket mean.

Each comes with a bootstrap interval. There is no PASS/FAIL flag.

## Multiplicity

The report states how many series x horizons were examined (currently 1 x 6). Each
regression, bucket and dose-response line is exploratory. A screen points at where a
benchmark question might be worth registering; it never replaces one.
