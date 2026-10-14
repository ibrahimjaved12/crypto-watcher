# Candle patterns

`market_analysis.patterns` is a pure feature/reading layer. `CandleFeatures`,
`PatternComponent`, and `PatternReading` are frozen records; `CandlePattern` is
a small protocol. There is no pattern registry or automatic discovery. Existing
ta-v2 detection, persisted meanings, and API contracts are unchanged on purpose.

## Inputs and point-in-time rules

Only completed 15m, 1h, and 4h benchmark candles are used. Their base volume and
taker-buy base volume sum the constituent 1m bars. The existing OHLC validity
rule applies to both quantities: a missing/compromised minute invalidates the
whole candle; no partial sums. Price-only callers retain unavailable quantities.

At candle i, range = high - low, body = abs(close - open), upper = high -
max(open, close), lower = min(open, close) - low. Each ratio divides by range.
ATR14 and ADX14 are the existing Wilder calculations through i-1; trend direction
is sign(EMA20 - EMA50) at i-1. Relative volume is current base volume divided by
the preceding 20-candle mean, excluding i. Prior high/low also exclude i.
No later candle is read for the decision. Gaps reset indicator initialization;
unready prior inputs, including a zero volume reference, yield `insufficient`.
Scaled-integer storage is retained; the adapter uses technical.py's float helpers.

## Frozen doji-v1

All constants live in frozen `DojiParams` in `patterns/doji.py`. The inclusive
gate requires positive range and ATR, body/range <= 0.10, and range/ATR >= 0.5.
Zero range is `insufficient`; other ready gate failures are `not_pattern`.
Subtypes are exclusive in this order: dragonfly (lower >= 0.60, upper <= 0.10),
gravestone (upper >= 0.60, lower <= 0.10), long_legged (both >= 0.30), standard.

| Component | Raw | Normalized before clipping | Weight |
|---|---|---|---|
| tightness | body/range | 1 - raw/0.10 | 0.20 |
| size | range/prior ATR14 | (raw - 0.5)/1.0 | 0.25 |
| volume | relative volume | (raw - 0.5)/1.5 | 0.20 |
| trend | prior ADX14 | (raw - 20)/20 | 0.20 |
| location | distance | 1 - raw | 0.15 |

Distance is max(0, prior20_high - current_high)/ATR for an upward trend, or
max(0, current_low - prior20_low)/ATR for a downward trend. A neutral trend has
raw `None`, normalization 0. Each normalization clips to [0,1]. Contribution is
100 * weight * normalization; score sums contributions. Every successful reading
returns all raw values, normalizations, weights, contributions, subtype and reason
tags. Scores below 50 are weak, [50,75) strong, and >=75 very_strong. Scores are
descriptive rankings, not win probabilities; unsuccessful readings have no score.

## Strategies and versioning

`doji_continuation_v1` and `doji_reversal_v1` are registered, **unvalidated**
benchmark variants. A ready doji with prior ADX >=20 and nonzero prior EMA trend
emits that direction or its opposite at candle close. Horizon equals timeframe.
Entry, barriers, expiry and costs use the existing label engine. There is no
next-candle confirmation rule or accuracy metric. The registry wrapper retains
`ta-v1`; new names and config `pattern_version: doji-v1` identify the new meanings.
Its existing four-field signal tuples have no per-signal score slot, so score-band
reporting is not wired; callers can obtain complete scores from pattern readings.

The two variants count in the private experiment log for multiple-testing
correction when evaluated. Follow-ups: benchmark development/validation only;
wire validated readings into TA with a version bump; add taker-flow scoring once
the live path carries taker-buy volume. Stored taker-buy volume is not scored yet.

ATR/EMA preparation is linear per valid run. The unchanged ADX helper returns
only its final value, so eligible geometry candidates require prior-prefix calls;
an all-doji run has quadratic ADX work. No runtime measurements were made.

To add a pattern, create one module with frozen parameters and a new version,
implement `CandlePattern.read`, and reuse the feature layer. Keep old definitions
immutable; changed constants or meaning require a new version. Register explicit
benchmark wrappers only when that new pattern is ready for evaluation.
