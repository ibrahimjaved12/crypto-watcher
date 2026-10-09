# Level-reaction event study L0

> **Provisional.** This document is AI-generated and is not the source of truth. The owner's
> intent in [`CLAUDE.md`](../CLAUDE.md) takes precedence. Ticket:
> [#185](https://github.com/ibrahimjaved12/crypto-watcher/issues/185).

L0 asks a descriptive question: when price comes back to a support or resistance level,
does it bounce more often than it would at an arbitrary price? It is not a strategy test:
there are no trades, labels, exits or PASS/FAIL verdicts. The code is in
`python/market_analysis/benchmark/levels.py` and `level_study.py`. The **Level study**
workflow (`.github/workflows/level-study.yml`) runs it on the development or validation
segment; the hidden guard refuses the hidden segment.

## Unit of distance

All distances are in the label engine's own point-in-time volatility. That is the EWMA
sigma (3-day half-life) for a 60- or 240-minute horizon, taken from the last completed
5-minute block at the decision minute, as a fraction of price. This is the same calibrated
unit as the sigma calibration audit (#220). Minutes before that sigma is published, and
minutes with a missing or compromised bar, take no part in the study.

## Level sets

All levels are point in time: each has a time from which it is known and may be used.

- **round**: a fixed grid per symbol. Spacing is 1000 USDT for BTC, 100 for ETH, 50 for BNB,
  10 for SOL, 0.10 for XRP and 0.01 for DOGE. BTC grid levels are also tagged by tier
  (10000, 5000 or 1000).
- **prev_day / prev_week**: the previous UTC day's (or Monday-start week's) high and low.
  Each is known from the start of the next day (week) and used during that day (week) only.
  A day or week with any missing or compromised minute gives no level.
- **swing4h**: fractal swing highs and lows on 4h candles, with 5 candles on each side.
  A swing is known only once the fifth confirming candle has closed and stays valid for 90
  days. An invalid candle anywhere in the window breaks the fractal.

## Events and outcomes

A level is approached from above (support) or from below (resistance).

- **Arming:** price is at least 1 sigma_240 away from the level on the approach side.
- **Event:** the first minute after arming whose extreme comes within 0.1 sigma_60 of the
  level. The arming must have happened within the previous 24 hours. Chop around a level
  counts once; a second event needs a new move 1 sigma_240 away.

From the event minute, the next 24 hours of minute extremes decide the status. A bounce
target sits 1 sigma_60 back on the approach side; a penetration barrier sits 0.25 sigma_60
through the level.

| Status | Meaning |
| --- | --- |
| B | Bounce target reached first |
| P | Penetration barrier reached first |
| T | Neither reached within 24 hours |
| A | Both reached in one minute, including the event minute itself. Reported as ambiguous, never resolved. |
| X | A missing or compromised minute came before resolution (exclusion) |
| I | The window runs past the segment end. Data after the segment is never read (exclusion). |

Forward returns enter at the open of the minute after the decision minute and exit at the
open 1, 2 and 4 hours and 1, 2 and 5 days later. They are reported in basis points and in
units of sigma_60 x sqrt(h / 60):

- **Touch drift:** from the event, positive in the bounce direction.
- **Penetration continuation:** from the penetration minute of P events, positive beyond
  the level.

A window that contains a missing or compromised minute, or passes the segment end, is
excluded and counted.

## Placebo

The same detector runs on artificial levels, so approach distance and volatility match by
construction:

- **round:** 20 shifted grids, each between 0.3 and 0.7 of the spacing away from the real
  grid. The shifts are reproducible from a fixed seed.
- **prev_day, prev_week and swing4h:** 20 levels per real level, at ±0.5 to ±1.4 sigma_240
  from it. The offsets are fixed when the real level is created, and each placebo level
  shares the real level's validity interval.

The BTC tier cells are compared with the whole BTC round placebo pool.

## What the report contains

The private report goes to `reports/levels/` in the research-data repo. Per segment it has:

- **Inputs:** the tercile thresholds and level counts per symbol.
- **Event counts:** real and placebo events per symbol and level set.
- **Cells:** one per level set x side x symbol (and pooled) x volatility tercile of
  sigma_240 at the event (and all). Tercile thresholds come from the development segment
  only. Each cell shows counts first: real and placebo events by status, plus exclusions.
  It then shows:
  - the bounce share B / (B + P), next to the driftless Brownian reference of 20%;
  - mean touch drift and penetration continuation per horizon;
  - real minus placebo for each, with a 95% interval from a UTC-day cluster bootstrap
    (2000 draws, deterministic seed).

The report states the number of cells. The study is exploratory screening: no single cell
is a hypothesis test, and the cell count is the multiplicity.

The public workflow log shows only symbol, level set, segment, real and placebo event
counts, and the report hash. It never shows rates or returns.

## Not in L0

L0 does not include:

- trades, labels, exits or a maker-entry model;
- clustering or merging of nearby levels;
- VWAP, pivot points or Fibonacci levels;
- a measured-move audit.

These are later slices, if L0 shows anything worth pursuing.
