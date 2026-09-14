# Calculation and rule contract (version 1)

Derived from `src/lib/market/providers.server.ts`, `quotes.server.ts`,
`symbols.ts`, and `src/lib/monitor/engine.server.ts` at local commit `c1bcb3c`.
This is an isolated analytical contract, not a replacement monitor.

## Existing implementation

| Concern | TypeScript behavior |
| --- | --- |
| Collection | Public GETs, Binance → OKX → Kraken; two requests per symbol/provider, 8s request timeout |
| Symbols | App allowlist of 20 USDT pairs; Binance `BTCUSDT`, OKX `BTC-USDT`, Kraken `BTCUSDT` (same mechanical conversion for other bases) |
| Candles | 61 one-minute and 97 fifteen-minute rows; open timestamps in ms, Kraken seconds multiplied by 1000; OKX reversed |
| Price | Last one-minute candle's close, including a potentially unfinished candle; no separate ticker |
| Changes | `(last.close - previous.close) / previous.close * 100`; row offsets 5/15/60 in 1m, 16/96 in 15m |
| Chart | Entire 97-row 15m response; no timestamp continuity check or completed-candle filtering |
| Stale | Latest 1m **open** timestamp older than 10 minutes; 15m series never checked independently |
| Rule | Absolute change >= positive configured threshold; defaults 2%, 15m window, enabled |
| Cooldown | Default 15 minutes, configurable; user + symbol + rule label, non-test alerts with triggered_at >= now minus cooldown |
| Precision | JS numbers; evaluate before rounding, store change rounded to four decimal places |

The rule is a **level test**, not detection of a crossing from below to above.
It can alert again after cooldown while still above threshold. Direction is not
part of the cooldown key. Changing threshold or window changes the rule label.
Exactly 15 minutes ago remains inside the existing inclusive cooldown query.
The Python threshold evaluator does not read cooldown history or authorize alerts.

## Python decisions and intentional differences

- Times are UTC integer epoch milliseconds. A bar covers `[open_ms, open_ms + interval)`;
  its price is observed at that end boundary. Open times must align to the interval.
- Use completed bars only: end <= analysis time; also honor OKX confirmation and
  exclude Kraken's final, uncommitted row. Binance close timestamp must equal
  open + interval - 1. No ticker or partial-candle value is presented as a completed close.
- Each window uses the newest completed bar in its own series, with the baseline
  exactly window_minutes earlier. Require all bars between endpoints. Sort input;
  reject duplicates, future opens, misalignment, nonfinite or nonpositive prices.
  Missing bars produce unavailable results; do not interpolate or silently shift time.
- The 1m and 15m windows can end at different times; each result includes both close
  timestamps and prices. This is completed-bar analysis, not a rolling change to a
  common live instant. A common-instant design would require different collection.
- Freshness is per interval: `lag = floor(as_of / interval) * interval - latest_close`.
  Lag > 10 minutes is stale; exactly 10 minutes is accepted. This intentionally
  measures delay behind the expected closed bar, so a normally current 15m candle
  does not become stale simply because its natural age exceeds ten minutes.
  Clock synchronization and the 10-minute allowance are explicit deployment assumptions.
- Use Decimal prices, 40-digit percentage calculation precision, no four-place rounding
  before evaluation, inclusive `abs(change) >= threshold`. JSON decimals are strings
  to preserve precision. Threshold must be finite and positive. Values immediately
  around a threshold are tested; arbitrary precision beyond 40 digits is not promised.
- Request 62 one-minute / 98 fifteen-minute rows to retain enough completed history
  after discarding the live bar. Kraken's endpoint returns its own bounded history.
- Preserve provider order, public endpoints and USDT symbol conventions. Require all
  five windows to be valid before accepting a provider; otherwise report its reasons
  and try the next provider. Never combine exchanges within a result. Failure is JSON
  with `ok: false` and nonzero exit status, never simulated data.

These freshness, complete-bar, gap and provider-acceptance rules are **proposed
analytical semantics**. The product does not specify them. Approve them before
using Python for production alerts. Partial-candle analysis is a possible product
choice, but existing row offsets do not establish exact elapsed-time comparisons
when bars are missing or the latest bar is unfinished.

The fixed fixture demonstrates a concrete difference: the completed 5m change is
2%; the app's current-row comparison yields approximately 49.7006% because its
newest unfinished close is 150 and its five-rows-back close is 100.2.

## Provider evidence and limits

[Binance kline documentation](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)
defines open/close timestamps and close price.
[OKX API documentation](https://app.okx.com/docs-v5/en/)
defines millisecond opening times and confirmation flags (0 unfinished, 1 completed).
[Kraken OHLC documentation](https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/)
defines seconds timestamps and states the final row is uncommitted; history is
limited to 720 entries.

USDT is preserved, but identical economic assets do not imply identical exchange
prices, listings, liquidity or available candles. The app's allowlist does not
prove each exchange lists each pair. Kraken aliases are left to the exchange;
unsupported pairs fail explicitly. No automatic USD substitution, renamed-token
substitution or exchange-to-exchange price comparison is performed. All 20 symbols
have not been live-verified.
