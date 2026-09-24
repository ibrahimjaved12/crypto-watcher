# Technical analysis

The shared Python package is the canonical implementation for manual analysis,
scheduled completed-candle TA, and chronological replay. TanStack remains the
scheduler, authorization boundary, market-data adapter, response validator, and
only database writer.

## Scheduled flow

For each enabled account and watched perpetual-futures contract, the monitor:

1. asks PostgreSQL which 15m, 1h, and 4h candles and forward outcomes are due;
2. fetches 250 trade-price candles only for frames with due work;
3. sends at most eight target candles per frame to the authenticated FastAPI
   `POST /v1/technical-analysis/batch` endpoint;
4. validates the complete versioned response and its contract, source, candle,
   evaluation, detection, and calculation-version fields; and
5. idempotently inserts `ta_signals` using the unique key
   `(user_id, symbol, timeframe, candle_at, version)`.

FastAPI calls `market_analysis.technical.calculate_technical_analysis`, the same
pure function used by manual Python analysis and `market_analysis.replay`. It has
no database credentials and performs no writes. A request times out after six
seconds and receives one bounded retry for network, timeout, rate-limit, or 5xx
failure. Invalid requests, authentication failures, redirects, and invalid
responses are not retried.

There is no alternate calculation path. Python configuration, transport,
validation, or calculation failures make the monitoring run partial or failed
with a visible `TA <symbol> <timeframe>m` error. Movement observations, cumulative
baselines, and movement alerts remain independent.

## Calculation contract

- Versions: `ta-v2` and `interpretation-v1`, request/response schema version 1.
- Instruments: supported linear USDT perpetual futures using trade-price candles.
- Timeframes: 15m, 1h, and 4h.
- Data: at least 200 completed, aligned, consecutive, valid OHLCV candles from a
  250-candle provider request; forming candles are excluded.
- Freshness: the latest provider candle may lag by at most one interval plus two
  minutes.
- Indicators: EMA20/50/200, RSI14, Wilder ATR14, MACD12/26/9, Bollinger20/2,
  Wilder ADX14/+DI/-DI, prior 20-candle range, and 20-candle volume comparison.
- Patterns: doji, hammer, shooting star, bullish/bearish engulfing, EMA20/50
  crosses, RSI 30/70 recovery/rejection, and volume spike.
- Interpretation: a descriptive score from -100 to +100 with persisted trend,
  momentum, pattern, and volume contributions and reasons. It is not a probability
  or an executable trade recommendation.

Delayed runs recover at most eight missing candles per frame, oldest first. Each
request names its target candle, so catch-up uses only history available through
that candle while preserving the actual source-event, evaluation, and detection
times.

## Persistence and outcomes

Each saved snapshot includes canonical and source contract identities, provider
and endpoint, price type, candle open/close event time, evaluation and detection
times, calculation and strategy versions, score, factor breakdown, reasons,
patterns, and indicators. The dashboard renders these persisted conclusions; it
does not recalculate a score in the browser.

Forward outcomes remain descriptive. The target is four intervals after the next
timeframe boundary at or after detection. TanStack updates pending rows
idempotently using the recorded provider source. A missing target outside the
available provider history becomes `unavailable`; a provider failure stays pending
for retry.

`completed_candle_ta_enabled`, `TA_GENERATION_ENABLED`, and
`TA_OUTCOME_EVALUATION_ENABLED` control scheduled work. `PYTHON_ANALYSIS_ENABLED`,
`PYTHON_ANALYSIS_URL`, and `PYTHON_ANALYSIS_TOKEN` must be configured for TA
generation. See [Python service](python-api.md) and
[activity controls](activity-controls.md).
