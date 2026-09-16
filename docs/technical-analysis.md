# Technical Analysis v1

TA runs inside the existing scheduled and manual monitoring pass for enabled
watchlists. Its records live in `ta_signals`; movement alerts and their baselines
remain separate. A TA failure is reported in monitoring run errors as `TA ...`
and does not prevent movement alert calculations.

## Calculation contract

- Timeframes: 15m, 1h, 4h, using exchange-native spot USDT candles.
- Fetch 250 bars per timeframe. Require at least 200 completed, consecutive,
  aligned candles with valid OHLC and nonnegative base-asset volume.
- Exclude forming candles. Reject missing or duplicate completed bars.
- Reject a last close older than one interval plus two minutes.
- Binance, OKX, then Kraken fallback; never stitch exchanges into one series.
- EMA 20/50, Wilder RSI 14 and Wilder ATR 14 use `technicalindicators` 3.1.0.
- Volume change compares the latest bar with the mean of the previous 20 bars,
  excluding the current bar. A zero mean produces null, not infinity.

Patterns use explicit v1 definitions; they are observations, not buy/sell orders:

| Pattern | Rule |
| --- | --- |
| Doji | Body <= 10% of high-low range; zero-range bars excluded |
| Hammer | Body > 10% of range, lower wick >= 2 bodies, upper wick <= 0.5 body, preceding three-bar close change negative |
| Shooting star | Mirrored hammer with positive preceding close change |
| Bullish/bearish engulfing | Opposite-colored bodies, current body fully covers previous body and is strictly larger |
| EMA cross | EMA20 crosses EMA50 on the latest completed candle |
| RSI recovery/rejection | Cross back above 30 or below 70 |
| Volume spike | Latest volume >= twice the previous 20-bar mean |

Each check saves the latest completed candle's snapshot, including observations
with no pattern. Unique key: user, symbol, timeframe, candle open time, version.
Concurrent and repeated checks preserve the first snapshot and source. The
version is `ta-v1`; future rule changes must increment it. Downtime is not
backfilled with signals that the user could not have seen at that time.

## Outcomes

The target is four intervals after the next timeframe boundary at or after
the actual detection time. Return is the target completed close divided by the
recorded signal close, minus one, expressed as a percentage. For example,
a 15m observation detected at 12:05 is evaluated at 13:15.

This is a descriptive forward price change, not a simulated executable trade,
direction-adjusted win rate, or profit after fees, slippage, and funding.
The starting close may precede detection. The original exchange alone is used
for outcomes, including when the current signal uses a fallback exchange.
Missing target history becomes `unavailable`; provider failures leave it pending
and are retried. Outcomes are processed while the pair remains watched and
monitoring is enabled. Removing or pausing a pair preserves history but pauses
its outcome updates. At most 500 pending records per pair/frame are handled
per run. No TP/SL, news score, or combined movement/TA score is implemented.

## Deployment

Apply `supabase/migrations/20260917090000_technical_analysis.sql` in Lovable before
deploying the app changes. Authenticated users may read only their own records;
only the server service role may create or update them.

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
[OKX candle format](https://app.okx.com/docs-v5/en).
