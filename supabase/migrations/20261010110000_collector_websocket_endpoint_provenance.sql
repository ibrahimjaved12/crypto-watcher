-- With the collector owning market data, TA conclusions carry the endpoint the collector recorded
-- for the candle (#24/#26): '/fapi/v1/klines' for REST candles and the Binance market stream for
-- candles finalized from the live websocket. The original provenance checks only allowed the REST
-- endpoint, so every TA row built from a websocket candle was rejected. Widen them for binance-usdm
-- only; OKX and Kraken stay REST-only and the price type stays 'trade'.
ALTER TABLE public.ta_signals
  DROP CONSTRAINT ta_signals_futures_provenance_check,
  ADD CONSTRAINT ta_signals_futures_provenance_check
    CHECK (price_type = 'trade' AND (
      (source = 'binance-usdm' AND endpoint IN (
        '/fapi/v1/klines', 'wss://fstream.binance.com/market/stream')) OR
      (source = 'okx-usdt-swap' AND endpoint = '/api/v5/market/candles') OR
      (source = 'kraken-futures' AND endpoint = '/api/charts/v1/trade/:symbol/:resolution')
    ));

ALTER TABLE public.analysis_conclusions
  DROP CONSTRAINT analysis_conclusions_provider_provenance_check,
  ADD CONSTRAINT analysis_conclusions_provider_provenance_check CHECK (
    price_type = 'trade' AND
    source_instrument_id = provider || ':' || source_native_symbol AND (
      (provider = 'binance-usdm' AND endpoint IN (
        '/fapi/v1/klines', 'wss://fstream.binance.com/market/stream')) OR
      (provider = 'kraken-futures' AND endpoint = '/api/charts/v1/trade/:symbol/:resolution') OR
      (provider = 'okx-usdt-swap' AND endpoint = '/api/v5/market/candles')
    )
  );
