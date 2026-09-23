-- The pre-release market data is disposable. Keep accounts and user settings,
-- but rebuild all market-linked state with an explicit futures identity.
DELETE FROM public.notes;
DELETE FROM public.alerts;
DELETE FROM public.monitor_runs;
DELETE FROM public.ta_signals;
DELETE FROM public.watchlist_items;

CREATE TABLE public.market_instruments (
  id TEXT PRIMARY KEY,
  exchange TEXT NOT NULL CHECK (exchange = 'binance'),
  native_symbol TEXT NOT NULL,
  market_type TEXT NOT NULL CHECK (market_type = 'futures'),
  contract_type TEXT NOT NULL CHECK (contract_type = 'perpetual'),
  base_asset TEXT NOT NULL,
  quote_asset TEXT NOT NULL CHECK (quote_asset = 'USDT'),
  margin_asset TEXT NOT NULL CHECK (margin_asset = 'USDT'),
  settlement_asset TEXT NOT NULL CHECK (settlement_asset = 'USDT'),
  is_linear BOOLEAN NOT NULL CHECK (is_linear),
  contract_multiplier NUMERIC NOT NULL CHECK (contract_multiplier = 1),
  UNIQUE (id, native_symbol),
  UNIQUE (exchange, native_symbol, market_type, contract_type)
);

INSERT INTO public.market_instruments
  (id, exchange, native_symbol, market_type, contract_type, base_asset,
   quote_asset, margin_asset, settlement_asset, is_linear, contract_multiplier)
VALUES
  ('binance-usdm:BTCUSDT', 'binance', 'BTCUSDT', 'futures', 'perpetual', 'BTC', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:ETHUSDT', 'binance', 'ETHUSDT', 'futures', 'perpetual', 'ETH', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:DOGEUSDT', 'binance', 'DOGEUSDT', 'futures', 'perpetual', 'DOGE', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:SOLUSDT', 'binance', 'SOLUSDT', 'futures', 'perpetual', 'SOL', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:XRPUSDT', 'binance', 'XRPUSDT', 'futures', 'perpetual', 'XRP', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:ADAUSDT', 'binance', 'ADAUSDT', 'futures', 'perpetual', 'ADA', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:BNBUSDT', 'binance', 'BNBUSDT', 'futures', 'perpetual', 'BNB', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:AVAXUSDT', 'binance', 'AVAXUSDT', 'futures', 'perpetual', 'AVAX', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:LINKUSDT', 'binance', 'LINKUSDT', 'futures', 'perpetual', 'LINK', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:POLUSDT', 'binance', 'POLUSDT', 'futures', 'perpetual', 'POL', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:DOTUSDT', 'binance', 'DOTUSDT', 'futures', 'perpetual', 'DOT', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:LTCUSDT', 'binance', 'LTCUSDT', 'futures', 'perpetual', 'LTC', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:TRXUSDT', 'binance', 'TRXUSDT', 'futures', 'perpetual', 'TRX', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:ATOMUSDT', 'binance', 'ATOMUSDT', 'futures', 'perpetual', 'ATOM', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:NEARUSDT', 'binance', 'NEARUSDT', 'futures', 'perpetual', 'NEAR', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:APTUSDT', 'binance', 'APTUSDT', 'futures', 'perpetual', 'APT', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:ARBUSDT', 'binance', 'ARBUSDT', 'futures', 'perpetual', 'ARB', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:OPUSDT', 'binance', 'OPUSDT', 'futures', 'perpetual', 'OP', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:SUIUSDT', 'binance', 'SUIUSDT', 'futures', 'perpetual', 'SUI', 'USDT', 'USDT', 'USDT', true, 1),
  ('binance-usdm:TONUSDT', 'binance', 'TONUSDT', 'futures', 'perpetual', 'TON', 'USDT', 'USDT', 'USDT', true, 1);

ALTER TABLE public.market_instruments ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.market_instruments TO anon, authenticated, service_role;
CREATE POLICY "public futures instruments" ON public.market_instruments FOR SELECT USING (true);

ALTER TABLE public.watchlist_items
  ADD COLUMN instrument_id TEXT NOT NULL,
  ADD CONSTRAINT watchlist_instrument_identity_fkey
    FOREIGN KEY (instrument_id, symbol)
    REFERENCES public.market_instruments(id, native_symbol),
  ADD CONSTRAINT watchlist_user_instrument_key UNIQUE (user_id, instrument_id);

ALTER TABLE public.notes
  ADD COLUMN instrument_id TEXT,
  ADD CONSTRAINT notes_instrument_pair_check
    CHECK ((instrument_id IS NULL) = (symbol IS NULL)),
  ADD CONSTRAINT notes_instrument_identity_fkey
    FOREIGN KEY (instrument_id, symbol)
    REFERENCES public.market_instruments(id, native_symbol);

ALTER TABLE public.alerts
  ADD COLUMN instrument_id TEXT NOT NULL,
  ADD COLUMN endpoint TEXT NOT NULL DEFAULT '/fapi/v1/klines',
  ADD COLUMN price_type TEXT NOT NULL DEFAULT 'trade',
  ADD CONSTRAINT alerts_instrument_identity_fkey
    FOREIGN KEY (instrument_id, symbol)
    REFERENCES public.market_instruments(id, native_symbol),
  ADD CONSTRAINT alerts_futures_provenance_check CHECK (
    (is_test AND data_source = 'test' AND price_type = 'not_applicable') OR
    (NOT is_test AND price_type = 'trade' AND (
      (data_source = 'binance-usdm' AND endpoint = '/fapi/v1/klines') OR
      (data_source = 'okx-usdt-swap' AND endpoint = '/api/v5/market/candles') OR
      (data_source = 'kraken-futures' AND endpoint = '/api/charts/v1/trade/:symbol/:resolution')
    ))
  );

ALTER TABLE public.monitor_baselines
  ADD COLUMN instrument_id TEXT NOT NULL,
  ADD COLUMN endpoint TEXT NOT NULL DEFAULT '/fapi/v1/klines',
  ADD COLUMN price_type TEXT NOT NULL DEFAULT 'trade',
  ADD CONSTRAINT monitor_baselines_instrument_identity_fkey
    FOREIGN KEY (instrument_id, symbol)
    REFERENCES public.market_instruments(id, native_symbol),
  ADD CONSTRAINT monitor_baselines_futures_provenance_check
    CHECK (price_type = 'trade' AND (
      (data_source = 'binance-usdm' AND endpoint = '/fapi/v1/klines') OR
      (data_source = 'okx-usdt-swap' AND endpoint = '/api/v5/market/candles') OR
      (data_source = 'kraken-futures' AND endpoint = '/api/charts/v1/trade/:symbol/:resolution')
    ));

ALTER TABLE public.market_data_checkpoints
  DROP CONSTRAINT market_data_checkpoints_data_source_check,
  ADD COLUMN instrument_id TEXT NOT NULL,
  ADD COLUMN endpoint TEXT NOT NULL DEFAULT '/fapi/v1/klines',
  ADD COLUMN price_type TEXT NOT NULL DEFAULT 'trade',
  ADD CONSTRAINT market_data_checkpoints_instrument_identity_fkey
    FOREIGN KEY (instrument_id, symbol)
    REFERENCES public.market_instruments(id, native_symbol),
  ADD CONSTRAINT market_data_checkpoints_futures_provenance_check
    CHECK (price_type = 'trade' AND (
      (data_source = 'binance-usdm' AND endpoint = '/fapi/v1/klines') OR
      (data_source = 'okx-usdt-swap' AND endpoint = '/api/v5/market/candles') OR
      (data_source = 'kraken-futures' AND endpoint = '/api/charts/v1/trade/:symbol/:resolution')
    ));

ALTER TABLE public.ta_signals
  ADD COLUMN instrument_id TEXT NOT NULL,
  ADD COLUMN endpoint TEXT NOT NULL DEFAULT '/fapi/v1/klines',
  ADD COLUMN price_type TEXT NOT NULL DEFAULT 'trade',
  ADD CONSTRAINT ta_signals_instrument_identity_fkey
    FOREIGN KEY (instrument_id, symbol)
    REFERENCES public.market_instruments(id, native_symbol),
  ADD CONSTRAINT ta_signals_futures_provenance_check
    CHECK (price_type = 'trade' AND (
      (source = 'binance-usdm' AND endpoint = '/fapi/v1/klines') OR
      (source = 'okx-usdt-swap' AND endpoint = '/api/v5/market/candles') OR
      (source = 'kraken-futures' AND endpoint = '/api/charts/v1/trade/:symbol/:resolution')
    ));

ALTER TABLE public.monitor_runs
  ADD CONSTRAINT monitor_runs_futures_source_check
    CHECK (data_source IS NULL OR data_source IN
      ('binance-usdm', 'okx-usdt-swap', 'kraken-futures'));

CREATE OR REPLACE FUNCTION public.record_market_data_checkpoint(
  p_user_id UUID, p_symbol TEXT, p_price NUMERIC,
  p_observed_at TIMESTAMPTZ, p_source TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  run_at TIMESTAMPTZ := clock_timestamp();
  changed_rows INTEGER;
  cfg public.monitor_settings%ROWTYPE;
  selected_instrument TEXT;
BEGIN
  IF p_price IS NULL OR NOT (p_price > 0 AND p_price < 'Infinity'::numeric)
     OR p_observed_at IS NULL OR NOT isfinite(p_observed_at)
     OR p_observed_at > run_at OR p_observed_at < run_at - interval '10 minutes'
     OR date_trunc('minute', p_observed_at) <> p_observed_at
     OR p_source IS NULL
     OR p_source NOT IN ('binance-usdm', 'okx-usdt-swap', 'kraken-futures') THEN
    RAISE EXCEPTION 'Invalid or stale completed futures candle checkpoint';
  END IF;

  SELECT instrument_id INTO selected_instrument FROM public.watchlist_items
    WHERE user_id = p_user_id AND symbol = p_symbol FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('status', 'not_watched'); END IF;

  SELECT * INTO cfg FROM public.monitor_settings WHERE user_id = p_user_id FOR SHARE;
  IF FOUND AND (NOT cfg.monitoring_enabled OR NOT cfg.market_data_collection_enabled) THEN
    RETURN jsonb_build_object('status', 'disabled');
  END IF;

  INSERT INTO public.market_data_checkpoints
    (user_id, symbol, instrument_id, observed_at, price, data_source, endpoint, price_type, updated_at)
    VALUES (p_user_id, p_symbol, selected_instrument, p_observed_at, p_price,
      p_source, CASE p_source
        WHEN 'binance-usdm' THEN '/fapi/v1/klines'
        WHEN 'okx-usdt-swap' THEN '/api/v5/market/candles'
        ELSE '/api/charts/v1/trade/:symbol/:resolution'
      END, 'trade', run_at)
  ON CONFLICT (user_id, symbol) DO UPDATE
    SET observed_at = EXCLUDED.observed_at,
        price = EXCLUDED.price,
        data_source = EXCLUDED.data_source,
        instrument_id = EXCLUDED.instrument_id,
        endpoint = EXCLUDED.endpoint,
        price_type = EXCLUDED.price_type,
        updated_at = run_at
    WHERE public.market_data_checkpoints.observed_at < EXCLUDED.observed_at;
  GET DIAGNOSTICS changed_rows = ROW_COUNT;
  RETURN jsonb_build_object(
    'status', CASE WHEN changed_rows = 0 THEN 'already_processed' ELSE 'recorded' END
  );
END;
$$;

CREATE OR REPLACE FUNCTION public.process_cumulative_observation(
  p_user_id UUID, p_symbol TEXT, p_price NUMERIC,
  p_observed_at TIMESTAMPTZ, p_source TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  s public.monitor_baselines%ROWTYPE;
  cfg public.monitor_settings%ROWTYPE;
  threshold NUMERIC := 2;
  cooldown INTEGER := 15;
  changed NUMERIC;
  direction TEXT;
  last_alert TIMESTAMPTZ;
  run_at TIMESTAMPTZ := clock_timestamp();
  saved_id UUID;
  selected_instrument TEXT;
BEGIN
  IF p_price IS NULL OR NOT (p_price > 0 AND p_price < 'Infinity'::numeric)
     OR p_observed_at IS NULL OR NOT isfinite(p_observed_at)
     OR p_observed_at > run_at OR p_observed_at < run_at - interval '10 minutes'
     OR date_trunc('minute', p_observed_at) <> p_observed_at
     OR p_source IS NULL
     OR p_source NOT IN ('binance-usdm', 'okx-usdt-swap', 'kraken-futures') THEN
    RAISE EXCEPTION 'Invalid or stale completed futures candle observation';
  END IF;

  SELECT instrument_id INTO selected_instrument FROM public.watchlist_items
    WHERE user_id = p_user_id AND symbol = p_symbol FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('status', 'not_watched'); END IF;

  SELECT * INTO cfg FROM public.monitor_settings WHERE user_id = p_user_id FOR SHARE;
  IF FOUND THEN
    IF NOT cfg.monitoring_enabled OR NOT cfg.market_data_collection_enabled
       OR NOT cfg.movement_alerts_enabled THEN
      RETURN jsonb_build_object('status', 'disabled');
    END IF;
    threshold := cfg.threshold_pct;
    cooldown := cfg.cooldown_minutes;
  END IF;
  IF NOT (threshold >= 0.1 AND threshold <= 100) OR cooldown < 1 OR cooldown > 1440 THEN
    RAISE EXCEPTION 'Invalid monitor threshold or cooldown';
  END IF;

  SELECT * INTO s FROM public.monitor_baselines
    WHERE user_id = p_user_id AND symbol = p_symbol FOR UPDATE;
  IF NOT FOUND THEN
    INSERT INTO public.monitor_baselines
      (user_id, symbol, instrument_id, baseline_price, baseline_at, data_source,
       endpoint, price_type, threshold_pct, last_observed_at)
      VALUES (p_user_id, p_symbol, selected_instrument, p_price, p_observed_at,
        p_source, CASE p_source
          WHEN 'binance-usdm' THEN '/fapi/v1/klines'
          WHEN 'okx-usdt-swap' THEN '/api/v5/market/candles'
          ELSE '/api/charts/v1/trade/:symbol/:resolution'
        END, 'trade', threshold, p_observed_at);
    RETURN jsonb_build_object('status', 'initialized');
  END IF;
  IF p_observed_at <= s.last_observed_at THEN
    RETURN jsonb_build_object('status', 'already_processed');
  END IF;
  IF p_source <> s.data_source OR selected_instrument <> s.instrument_id
     OR threshold <> s.threshold_pct THEN
    UPDATE public.monitor_baselines SET baseline_price = p_price, baseline_at = p_observed_at,
      data_source = p_source, instrument_id = selected_instrument,
      endpoint = CASE p_source
        WHEN 'binance-usdm' THEN '/fapi/v1/klines'
        WHEN 'okx-usdt-swap' THEN '/api/v5/market/candles'
        ELSE '/api/charts/v1/trade/:symbol/:resolution'
      END, price_type = 'trade', threshold_pct = threshold,
      last_observed_at = p_observed_at
      WHERE user_id = p_user_id AND symbol = p_symbol;
    RETURN jsonb_build_object('status', 'reinitialized');
  END IF;

  UPDATE public.monitor_baselines SET last_observed_at = p_observed_at
    WHERE user_id = p_user_id AND symbol = p_symbol;
  changed := (p_price - s.baseline_price) / s.baseline_price * 100;
  IF abs(p_price - s.baseline_price) * 100 < s.baseline_price * threshold THEN
    RETURN jsonb_build_object('status', 'below_threshold', 'change_pct', changed);
  END IF;
  direction := CASE WHEN p_price > s.baseline_price THEN 'up' ELSE 'down' END;
  last_alert := CASE WHEN direction = 'up' THEN s.last_up_alert_at ELSE s.last_down_alert_at END;
  IF last_alert IS NOT NULL AND run_at < last_alert + make_interval(mins => cooldown) THEN
    RETURN jsonb_build_object('status', 'cooldown', 'direction', direction);
  END IF;

  INSERT INTO public.alerts (user_id, symbol, instrument_id, change_pct, window_minutes,
    threshold_pct, rule, price, data_source, endpoint, price_type, is_test, comparison_mode,
    baseline_price, baseline_at, observed_at, triggered_at)
    VALUES (p_user_id, p_symbol, selected_instrument, round(changed, 4), NULL, threshold,
      format('%s >= %s%% from saved baseline', direction, threshold), p_price,
      p_source, CASE p_source
        WHEN 'binance-usdm' THEN '/fapi/v1/klines'
        WHEN 'okx-usdt-swap' THEN '/api/v5/market/candles'
        ELSE '/api/charts/v1/trade/:symbol/:resolution'
      END, 'trade', false, 'baseline', s.baseline_price,
      s.baseline_at, p_observed_at, run_at)
    RETURNING id INTO saved_id;
  UPDATE public.monitor_baselines SET baseline_price = p_price, baseline_at = p_observed_at,
    last_up_alert_at = CASE WHEN direction = 'up' THEN run_at ELSE last_up_alert_at END,
    last_down_alert_at = CASE WHEN direction = 'down' THEN run_at ELSE last_down_alert_at END
    WHERE user_id = p_user_id AND symbol = p_symbol;
  RETURN jsonb_build_object('status', 'alerted', 'alert_id', saved_id, 'direction', direction,
    'change_pct', changed);
END;
$$;
