# Technical Analysis v2

This document specifies the current descriptive TA implementation. Future strategy
versions may use these observations as the originating evidence for a conditional
futures setup. The saved pattern and assessment must remain linked to the setup's
eligibility, confirmation, continued validity, entry, management, expiry, and
event-ordered outcome rules defined in the
[analysis and evaluation specification](analysis-evaluation.md).

## v2 coverage and architecture

Saved TA runs in the existing TypeScript monitoring pipeline. Python remains the
separate read-only rolling-price/baseline service; this release does not migrate
TA to it. The existing pinned indicator library supplies the added calculations.
No schema migration or extra exchange requests are required: new values use the
existing `ta_signals.indicators` JSON field. Historical records are not rewritten.

| Feature           | v2 behavior                                                                      |
| ----------------- | -------------------------------------------------------------------------------- |
| RSI               | Wilder RSI14 and threshold explanations                                          |
| Moving averages   | EMA20/50 plus EMA200 long-term price context                                     |
| MACD              | EMA12 minus EMA26, EMA9 signal, histogram and previous histogram                 |
| Bollinger Bands   | 20-close SMA ± 2 population standard deviations, bandwidth percent, %B           |
| Volatility        | Wilder ATR14 and percent of close                                                |
| Volume            | Latest / previous 20-bar mean ratio and percentage change                        |
| Timeframes        | 15m, 1h, 4h and an All history filter                                            |
| Direction         | Existing EMA trend and separate EMA200/MACD bullish, bearish or neutral context  |
| Explanations      | Expand Indicator explanations for values, formulas, thresholds and pattern rules |
| Completed candles | Existing aligned, consecutive, fresh OHLCV validation                            |
| Trend strength    | Wilder ADX14 and +DI/−DI: below 20 weak, 20–25 developing, >=25 trending         |
| Range context     | Previous 20 candles' lowest low and highest high, excluding the latest candle    |

ADX adds strength context without assigning a direction. Range levels provide
reference boundaries, not guaranteed support/resistance. Additional oscillators,
automated divergence, strategy rules and backtesting are deferred. MACD/EMA/RSI
are correlated, so agreement is not independent confirmation. The added indicators
do not contribute points to the existing interpretation-v1 score.

MACD bias uses histogram sign: rising negative histogram still means bearish bias
with improving momentum. Band location does not guarantee reversal. %B is null
when bands coincide; zero reference volume produces null ratio/change. Undefined
or nonfinite v2 outputs are stored as null. EMA200 uses finite available history
(minimum 200 bars), so may differ from a chart initialized with much more history.
All periods refer to candles, not days. Range breaks require a strictly outside
close; equality remains inside. ADX thresholds are descriptive heuristics.

New snapshots use ta-v2 and include candle count. Historical v1 values missing
new fields display unavailable. The All filter shows paginated history rather
than a synchronized cross-timeframe score; compare close times and sources.

TA runs inside the existing scheduled and manual monitoring pass for enabled
watchlists. Its records live in `ta_signals`; movement alerts and their baselines
remain separate. The user-scoped Completed-candle technical analysis control can
pause both new snapshots and pending outcome work without pausing movement alerts;
market-data collection and the monitoring master switch remain prerequisites. A TA
failure is reported in monitoring run errors as `TA ...`
and does not prevent movement alert calculations. Temporary activity controls can
independently pause new TA generation (`TA_GENERATION_ENABLED=false`), pending
outcome evaluation (`TA_OUTCOME_EVALUATION_ENABLED=false`), or automatic browser
history loading. Browser history loading defaults to manual-only and requires
`VITE_TA_HISTORY_AUTO_REFRESH_ENABLED=true` to run automatically. See
[activity controls](activity-controls.md) for deployment flags and
[independent activity controls](activity-domains.md) for user settings.

## Calculation contract

- Timeframes: 15m, 1h, 4h, using perpetual-futures trade-price candles.
- Fetch 250 bars per timeframe. Require at least 200 completed, consecutive,
  aligned candles with valid OHLC and nonnegative base-asset volume.
- Exclude forming candles. Reject missing or duplicate completed bars.
- Reject a last close older than one interval plus two minutes.
- Try Binance USDⓈ-M, OKX USDT swaps, then Kraken perpetuals.
- EMA20/50/200, MACD12/26/9, Bollinger20/2, Wilder ADX14, RSI14 and ATR14
  use `technicalindicators` 3.1.0.
- Volume change compares the latest bar with the mean of the previous 20 bars,
  excluding the current bar. A zero mean produces null, not infinity.

Patterns use explicit v1 definitions; they are observations, not buy/sell orders:

| Pattern                   | Rule                                                                                                           |
| ------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Doji                      | Body <= 10% of high-low range; zero-range bars excluded                                                        |
| Hammer                    | Body > 10% of range, lower wick >= 2 bodies, upper wick <= 0.5 body, preceding three-bar close change negative |
| Shooting star             | Mirrored hammer with positive preceding close change                                                           |
| Bullish/bearish engulfing | Opposite-colored bodies, current body fully covers previous body and is strictly larger                        |
| EMA cross                 | EMA20 crosses EMA50 on the latest completed candle                                                             |
| RSI recovery/rejection    | Cross back above 30 or below 70                                                                                |
| Volume spike              | Latest volume >= twice the previous 20-bar mean                                                                |

Each check saves the latest completed candle's snapshot, including observations
with no pattern. Unique key: user, symbol, timeframe, candle open time, version.
Concurrent and repeated checks preserve the first snapshot and source. The
version is `ta-v2`; future rule changes must increment it. Downtime is not
backfilled with signals that the user could not have seen at that time.

## Dashboard interpretation

`interpretation-v1` is calculated from each saved snapshot in the browser. It
requires no migration or new exchange requests, and applies to existing history.
It is a descriptive heuristic, not a calibrated probability or a trading strategy.
Scores are not persisted and must not be treated as historically issued advice.

- Trend: bullish when price > EMA20 > EMA50; bearish when price < EMA20 < EMA50;
  neutral otherwise, including equality. Contribution: +40, -40, or 0.
- Momentum: RSI <30 oversold (-20); [30,45) weak (-10); [45,55] neutral (0);
  (55,70] strong (+10); >70 overbought (+20). Extremes describe current momentum,
  not an automatic reversal prediction.
- ATR percent: 100 * ATR14 / saved close. It contributes no directional points.
- Directional patterns: hammer, bullish engulfing, bullish EMA cross, RSI recovery
  are bullish; shooting star, bearish engulfing, bearish EMA cross, RSI rejection
  are bearish. One-sided evidence contributes +20/-20 total, regardless of count.
  Conflicting evidence, doji, volume spike alone, and unknown patterns score 0.
- Volume adds +20/-20 only when a directional pattern agrees with the EMA trend
  and volume is at least the prior 20-bar average (volume change >=0).
- Pattern support labels distinguish conflicting directions, countertrend or
  neutral trend, missing inputs, below-average volume, and aligned plus volume
  supported. This assesses same-candle evidence, not later-candle confirmation.
- Total score ranges from -100 to +100. Expand a score to see all contributions.
  Missing/invalid trend, RSI, or volume inputs produce an unavailable score.
  Missing ATR only affects the volatility display. Movement alerts do not enter
  this calculation. Historical rows use the currently displayed interpretation
  version, independently of their original TA calculation version.

## Outcomes

The target is four intervals after the next timeframe boundary at or after
the actual detection time. Return is the target completed close divided by the
recorded signal close, minus one, expressed as a percentage. For example,
a 15m observation detected at 12:05 is evaluated at 13:15.

This is a descriptive forward price change, not a simulated executable trade,
direction-adjusted win rate, or profit after fees, slippage, and funding.
The starting close may precede detection. Outcomes use the same
`binance-usdm:<symbol>` contract and trade-price candle source as the saved signal.
Missing target history becomes `unavailable`; provider failures leave it pending
and are retried. Outcomes are processed while the pair remains watched and
monitoring is enabled. Removing or pausing a pair preserves history but pauses
its outcome updates. At most 500 pending records per pair/frame are handled
per run. No TP/SL, news score, or combined movement/TA score is implemented.

## Deployment

For an installation without TA v1, apply
`supabase/migrations/20260917090000_technical_analysis.sql` in Lovable before
deploying the app changes. Upgrading an existing TA v1 installation needs no
migration. Authenticated users may read only their own records;
only the server service role may create or update them.

After publication, let the existing monitor complete a check. Verify new ta-v2
rows in 15m/1h/4h, and expand Indicator explanations to check EMA200, MACD,
Bollinger, ADX and range values. Compare source and completed-close timestamps;
use All to inspect frames together. Old ta-v1 rows should remain readable with
unavailable new fields. Refresh reloads saved history; it does not run analysis.
When automatic TA history is disabled, the first load, filter changes, pagination,
focus/reconnect, and invalidations stay quiet until Refresh is pressed.

The existing cron endpoint also runs TA. Confirm the cron HTTP timeout and hosting
request budget allow the extra exchange requests: three per pair on the happy
path, with more on fallback or when settling a different exchange's outcomes.
The default pg_net timeout can be too short. Configure the existing job's
`net.http_post` timeout for the measured deployed runtime, without creating a
second overlapping job. Inspect HTTP results and `monitor_runs` after deployment.

Run `npm test --prefix tests`, `npx tsc --noEmit`, and `npm run build` with Node 22.
The dashboard displays saved history with timeframe and symbol filters. No paid
API, LLM key, or Python deployment is required for this module.

References: [technicalindicators](https://github.com/anandanand84/technicalindicators),
[OKX candles](https://www.okx.com/docs-v5/en/), and
[Kraken Futures candles](https://docs.kraken.com/api/docs/futures-api/charts/candles).

## Shared Python calculation package

The deterministic Python port is available through
`market_analysis.technical.calculate_technical_analysis`. FastAPI's authenticated
`/v1/technical-analysis` adapter and the offline chronological replay runner use the
same pure function and explicit futures identity, candles, timestamps, price type,
gap markers, and versions. The existing TypeScript scheduler and writers remain the
production path until their separate cutover.
