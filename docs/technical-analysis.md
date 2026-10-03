# Technical analysis

The shared Python package is the canonical implementation for manual analysis,
scheduled completed-candle TA, and chronological replay. TanStack remains the
scheduler, authorization boundary, market-data adapter, response validator, and
only database writer.

## Pre-release provenance cutover (destructive)

The provenance schema — `schema_version: 2` with a nullable `source_event_time_ms` —
is a **destructive pre-release reset, not a production-compatible migration**. Rows
written under the old synthetic-boundary semantics are disposable development data:

- the operational database wipes `collector_recent_candles`, `collector_health`, and
  `collector_leases`; the collector then rebuilds the required completed history
  through its REST bootstrap;
- every `ta_signals` row is cleared before the corrected constraint is applied.

No compatibility column, legacy-version marker, or dual timestamp semantics is kept.
The TA formula (`ta-v2`) and strategy (`interpretation-v1`) versions are unchanged
because the calculations did not change.

## Scheduled flow

For each enabled account and watched perpetual-futures contract, the monitor:

1. asks PostgreSQL which 15m, 1h, and 4h candles and forward outcomes are due;
2. loads completed trade-price candle history only for frames with due work — from
   the exchange provider normally, or from the operational collector store through
   the read-only adapter while `BINANCE_COLLECTOR_ENABLED=true`;
3. sends at most eight target candles per frame to the authenticated FastAPI
   `POST /v1/technical-analysis/batch` endpoint, carrying the source event time the
   input recorded (absent when the source has none) rather than one recomputed from
   the candle open;
4. validates the complete versioned response and its contract, source, candle,
   evaluation, detection, and calculation-version fields; and
5. idempotently inserts `ta_signals` using the unique key
   `(user_id, symbol, timeframe, candle_at, version)`, persisting the exact
   per-candle endpoint and source event time that produced the conclusion.

While the collector owns market data, the monitor never fetches a second live
exchange candle series: missing or stale canonical collector history fails the
affected frame visibly instead of falling back to another source. The read adapter
carries each candle's recorded provenance — endpoint, transport, candle close time,
the exchange event time when one exists (absent for REST) and receive time — so
WebSocket live candles are never persisted with REST provenance. See
[the collector design](./binance-futures-collector.md).

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

- Versions: `ta-v2` and `interpretation-v1`, request/response schema version 2.
- Instruments: supported linear USDT perpetual futures using trade-price candles.
- Timeframes: 15m, 1h, and 4h.
- Data: at least 200 completed, aligned, consecutive, valid OHLCV candles from a
  bounded provider/bootstrap request; forming candles are excluded.
- Provenance: `source_event_time_ms` is the actual exchange event time (WebSocket
  `E`) or `null` when the source has none (REST bootstrap/recovery and the manual
  REST path). It is never synthesized from the completion boundary.
- Freshness: the latest provider candle may lag by at most one interval plus two
  minutes.
- Finality: a target candle is complete at evaluation time when
  `target_open + timeframe <= evaluation_time`. The exchange event time is
  provenance only and never defines completion; an absent event time (REST
  bootstrap/recovery) is valid.
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

## History policies

These existing bounds have different owners and purposes:

| Value | Owner and purpose |
| --- | --- |
| 200 completed candles | Python TA minimum valid calculation history |
| Newest 260 completed candles per canonical series | Operational DB protected storage floor, an availability/resource margin independent of age-based retention |
| 300 klines per frame | Collector bootstrap request where implemented; the developing candle is excluded |
| 260 completed candles | Application TA read horizon where implemented |

Changing only stored surplus while consumers receive identical inputs is science-neutral. Changing
the actual read/input prefix can change EMA/Wilder-style recursive indicators through initialization
history. Storage-floor changes need history/catch-up evidence; input-prefix changes also need output
impact validation. These numbers and TA input semantics remain unchanged. The floor does not establish
seven-day movement normalization coverage; see [candle retention](./operational-database.md#reads-retention-and-synchronization).

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
