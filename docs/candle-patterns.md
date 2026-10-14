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
next-candle confirmation rule. The registry wrapper retains
`ta-v1`; new names and config `pattern_version: doji-v1` identify the new meanings.
Its existing four-field signal tuples have no per-signal score slot. The report
joins complete pattern readings to those signals and the existing label rows.

The two variants count in the private experiment log for multiple-testing
correction when evaluated. Follow-ups: benchmark development/validation only;
wire validated readings into TA with a version bump; add taker-flow scoring once
the live path carries taker-buy volume. Stored taker-buy volume is not scored yet.

ATR/EMA/ADX preparation is linear per valid run. The ADX series preserves the
existing helper's prefix arithmetic exactly; the existing helper and ta-v2 code
path are unchanged. Gaps reset initialization. No runtime measurements were made.

## Private doji report

Dispatch **Doji report** (`.github/workflows/doji-report.yml`) on main, choosing
development or validation, a subset of the six frozen symbols, and horizons
15/60/240 minutes. Run the two segments separately; the calibration hidden guard
refuses hidden before checkout or downloads. The workflow uses the calibration
audit's existing `RESEARCH_DATA_REPOSITORY` setting and `RESEARCH_DATA_TOKEN`
secret, manifest checks, guarded loading and private commit/push transport. It
uses the existing lb1 labels-v1/EWMA grid and rd release revision 1; no labels are
generated or changed.

Only metadata, counts and the private path are public. `report.md`, `events.csv`
and `summary.json` live under `reports/doji/<segment>__<hash>/` in the private
research-data repo. CSV includes every emitted signal at headline k=1, rr=1,
including non-trade and purged statuses; summary JSON includes all grid cells.
Each event retains the five score contributions and doji geometry, entry,
barriers, label outcome, gross R and net R at 1x/2x costs. Samples use up to 25
nearest unique events to evenly spaced UTC times per tier, with stable tie breakers, never
best/worst selection. Samples show direction / gross / net verdicts in that order.

**True/false is stop-aware after costs for profitability:** net verdict is true
only for measured positive net R, false for zero/negative R, ambiguous for X.
Gross verdict applies the same rule before costs and funding. Non-trade statuses
get no verdict. Direction is a separately named diagnostic: side times
ln(open[e+h]/open[e]) >0 / <0 / =0 yields true / false / tie, with e the minute
opening at the candle-close decision time (the minute after the final signal
bar), and h the timeframe. Ties are counted and excluded from direction rates.

Primary tables pool symbols and tiers per strategy/timeframe/side. Direction is
shown against the unconditional rate over all valid completed candles in the
same symbols/timeframe/segment. Trade tables count X as a loss in hit rates,
exclude X from R means and show both ambiguity and non-trade counts. They show
the existing 1/(1+rr) target-first barrier reference alongside T/(T+S), plus the
unconditional same-side label-grid profitability rate. These references have
different denominators; the Brownian barrier reference is not an expected net
profitability rate. Overlapping outcomes make naive Wilson 95% CIs optimistic.

Tier/subtype/symbol/ADX-band tables, the full stop/target grid and MFE/MAE
quantiles (50/75/90/95/99) are descriptive; tables flag fewer than 100 trades.
MFE/MAE use all 1m highs/lows in inclusive [e,e+h], even after trade exit, in
k times horizon sigma and percent of entry. Missing paths are counted, not
filled. Shares reaching MAE >=100/L percent for L=3/5/10/20 ignore maintenance
margin and funding; they are not simulated liquidations.

The single-writer private merge appends to `experiments/doji-report-v1.jsonl`
with the existing `ExperimentLog` replay convention. Each strategy/timeframe
has four separately counted primary hypotheses: A direction and B trade, each
long/short. Descriptive slices/grid cells do not add primary hypotheses.

To add a pattern, create one module with frozen parameters and a new version,
implement `CandlePattern.read`, and reuse the feature layer. Keep old definitions
immutable; changed constants or meaning require a new version. Register explicit
benchmark wrappers only when that new pattern is ready for evaluation.
