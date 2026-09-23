# Calculation and futures-data contract (version 1)

This is the standalone rolling-analysis contract. The application saved-baseline
rule remains documented in [cumulative monitoring](../docs/cumulative-monitoring.md).

## Instrument and provider

- Sources, in order: `binance-usdm`, `okx-usdt-swap`, `kraken-futures`.
- Market: perpetual futures only.
- Identity: `binance-usdm:<native symbol>`, for example
  `binance-usdm:BTCUSDT`.
- Binance and OKX contract metadata is validated before their candles are accepted.
- Candles use the contract trade price. Provider,
  endpoint, price type, event time, and retrieval time are explicit in results.
- Unsupported contracts fall through to the next futures provider and then fail visibly.

## Candle and calculation rules

- Request 62 one-minute and 98 fifteen-minute rows so a forming row can be removed
  while retaining the required completed history.
- Binance close time must equal `open + interval - 1`; other provider timestamps must align.
- Use completed, aligned, contiguous candles only. Reject duplicates, gaps, future
  opens, and nonfinite or nonpositive prices.
- Each window uses the newest completed candle in its series and the completed close
  exactly 5m, 15m, 1h, 4h, or 24h earlier.
- Freshness is checked per interval. More than ten minutes behind the expected close
  is stale; exactly ten minutes is accepted.
- Use decimal arithmetic and evaluate the inclusive
  `abs(change) >= threshold` boundary before presentation rounding.
- Missing or invalid data returns a structured unavailable result. No price is
  simulated or interpolated.

The one-shot CLI does not inspect cooldown history or authorize an alert. A threshold
match is descriptive analysis only.

Official references: [Binance USDⓈ-M](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data), [OKX](https://www.okx.com/docs-v5/en/), and [Kraken Futures](https://docs.kraken.com/api/docs/futures-api/charts/candles).

## Deterministic TA v2 contract

`market_analysis.technical.calculate_technical_analysis` accepts an immutable
`TechnicalInput`: exact perpetual-futures identity and source, timeframe, completed
OHLCV plus separate warm-up candles, explicit gap markers, source/evaluation/detection
timestamps, price type, and calculation versions. It supports 15m, 1h, and 4h and
never reads the clock or performs I/O.

The result preserves TA v2 indicators and interpretation-v1 scoring and adds a
compact status, bullish/bearish/neutral classification and direction, factor reason
tags, caller timestamps, and provenance. Fewer than 200 visible completed candles is
`insufficient`; malformed, gapped, stale, mismatched spot/futures, or unsupported
versioned input is `unavailable`. Forming and future candles are not visible to a
calculation. The offline replay adapter additionally removes them before every
chronological step.

This port does not change the TypeScript scheduled monitor or its database writes.
That implementation remains active until the planned scheduling cutover.
