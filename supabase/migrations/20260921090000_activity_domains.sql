-- User-scoped controls for independent monitoring activities. Current activities
-- retain their existing behavior by default; future engines and delivery channels
-- fail closed until their implementing issues are deployed.
ALTER TABLE public.monitor_settings
  ADD COLUMN market_data_collection_enabled BOOLEAN NOT NULL DEFAULT true,
  ADD COLUMN completed_candle_ta_enabled BOOLEAN NOT NULL DEFAULT true,
  ADD COLUMN movement_alerts_enabled BOOLEAN NOT NULL DEFAULT true,
  ADD COLUMN developing_setup_evaluation_enabled BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN paper_trading_enabled BOOLEAN NOT NULL DEFAULT false;

-- Transitional REST-collection checkpoint. The future shared WebSocket collector
-- may move this domain as a whole, but this table gives today's collector a state
-- independent of the movement baseline and downstream analysis consumers.
CREATE TABLE public.market_data_checkpoints (
  user_id UUID NOT NULL,
  symbol TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  price NUMERIC NOT NULL CHECK (price > 0 AND price < 'Infinity'::numeric),
  data_source TEXT NOT NULL CHECK (data_source IN ('Binance', 'OKX', 'Kraken')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, symbol),
  FOREIGN KEY (user_id, symbol) REFERENCES public.watchlist_items(user_id, symbol)
    ON DELETE CASCADE
);
ALTER TABLE public.market_data_checkpoints ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.market_data_checkpoints TO authenticated;
GRANT ALL ON public.market_data_checkpoints TO service_role;
CREATE POLICY "own market checkpoints"
  ON public.market_data_checkpoints FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

CREATE FUNCTION public.record_market_data_checkpoint(
  p_user_id UUID, p_symbol TEXT, p_price NUMERIC,
  p_observed_at TIMESTAMPTZ, p_source TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  run_at TIMESTAMPTZ := clock_timestamp();
  changed_rows INTEGER;
  cfg public.monitor_settings%ROWTYPE;
BEGIN
  IF p_price IS NULL OR NOT (p_price > 0 AND p_price < 'Infinity'::numeric)
     OR p_observed_at IS NULL OR NOT isfinite(p_observed_at)
     OR p_observed_at > run_at OR p_observed_at < run_at - interval '10 minutes'
     OR date_trunc('minute', p_observed_at) <> p_observed_at
     OR p_source IS NULL OR p_source NOT IN ('Binance', 'OKX', 'Kraken') THEN
    RAISE EXCEPTION 'Invalid or stale completed-candle checkpoint';
  END IF;

  PERFORM 1 FROM public.watchlist_items
    WHERE user_id = p_user_id AND symbol = p_symbol FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('status', 'not_watched'); END IF;

  SELECT * INTO cfg FROM public.monitor_settings WHERE user_id = p_user_id FOR SHARE;
  IF FOUND AND (NOT cfg.monitoring_enabled OR NOT cfg.market_data_collection_enabled) THEN
    RETURN jsonb_build_object('status', 'disabled');
  END IF;

  INSERT INTO public.market_data_checkpoints
    (user_id, symbol, observed_at, price, data_source, updated_at)
    VALUES (p_user_id, p_symbol, p_observed_at, p_price, p_source, run_at)
  ON CONFLICT (user_id, symbol) DO UPDATE
    SET observed_at = EXCLUDED.observed_at,
        price = EXCLUDED.price,
        data_source = EXCLUDED.data_source,
        updated_at = run_at
    WHERE public.market_data_checkpoints.observed_at < EXCLUDED.observed_at;
  GET DIAGNOSTICS changed_rows = ROW_COUNT;
  RETURN jsonb_build_object(
    'status', CASE WHEN changed_rows = 0 THEN 'already_processed' ELSE 'recorded' END
  );
END;
$$;
REVOKE ALL ON FUNCTION public.record_market_data_checkpoint(UUID, TEXT, NUMERIC, TIMESTAMPTZ, TEXT)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.record_market_data_checkpoint(UUID, TEXT, NUMERIC, TIMESTAMPTZ, TEXT)
  TO service_role;

CREATE TABLE public.notification_channel_preferences (
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  channel TEXT NOT NULL CHECK (channel IN ('email', 'whatsapp')),
  delivery_enabled BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, channel)
);
ALTER TABLE public.notification_channel_preferences ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.notification_channel_preferences TO authenticated;
GRANT ALL ON public.notification_channel_preferences TO service_role;
CREATE POLICY "own notification preferences"
  ON public.notification_channel_preferences FOR ALL TO authenticated
  USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE TRIGGER notification_preferences_updated_at
  BEFORE UPDATE ON public.notification_channel_preferences
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- Keep the transaction-level pause check authoritative. The server avoids calling
-- this RPC when movement alerts are paused; this re-read closes the race where a
-- user pauses collection or alerts while an already-started request is in flight.
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
BEGIN
  IF p_price IS NULL OR NOT (p_price > 0 AND p_price < 'Infinity'::numeric)
     OR p_observed_at IS NULL OR NOT isfinite(p_observed_at)
     OR p_observed_at > run_at OR p_observed_at < run_at - interval '10 minutes'
     OR date_trunc('minute', p_observed_at) <> p_observed_at
     OR p_source IS NULL OR p_source NOT IN ('Binance', 'OKX', 'Kraken') THEN
    RAISE EXCEPTION 'Invalid or stale completed-candle observation';
  END IF;

  PERFORM 1 FROM public.watchlist_items
    WHERE user_id = p_user_id AND symbol = p_symbol FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('status', 'not_watched'); END IF;

  SELECT * INTO cfg FROM public.monitor_settings WHERE user_id = p_user_id FOR SHARE;
  IF FOUND THEN
    IF NOT cfg.monitoring_enabled
       OR NOT cfg.market_data_collection_enabled
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
      (user_id, symbol, baseline_price, baseline_at, data_source, threshold_pct, last_observed_at)
      VALUES (p_user_id, p_symbol, p_price, p_observed_at, p_source, threshold, p_observed_at);
    RETURN jsonb_build_object('status', 'initialized');
  END IF;
  IF p_observed_at <= s.last_observed_at THEN
    RETURN jsonb_build_object('status', 'already_processed');
  END IF;
  IF p_source <> s.data_source OR threshold <> s.threshold_pct THEN
    UPDATE public.monitor_baselines SET baseline_price = p_price, baseline_at = p_observed_at,
      data_source = p_source, threshold_pct = threshold, last_observed_at = p_observed_at
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

  INSERT INTO public.alerts (user_id, symbol, change_pct, window_minutes, threshold_pct,
    rule, price, data_source, is_test, comparison_mode, baseline_price, baseline_at,
    observed_at, triggered_at)
    VALUES (p_user_id, p_symbol, round(changed, 4), NULL, threshold,
      format('%s >= %s%% from saved baseline', direction, threshold),
      p_price, p_source, false, 'baseline', s.baseline_price, s.baseline_at, p_observed_at, run_at)
    RETURNING id INTO saved_id;
  UPDATE public.monitor_baselines SET baseline_price = p_price, baseline_at = p_observed_at,
    last_up_alert_at = CASE WHEN direction = 'up' THEN run_at ELSE last_up_alert_at END,
    last_down_alert_at = CASE WHEN direction = 'down' THEN run_at ELSE last_down_alert_at END
    WHERE user_id = p_user_id AND symbol = p_symbol;
  RETURN jsonb_build_object('status', 'alerted', 'alert_id', saved_id, 'direction', direction,
    'change_pct', changed);
END;
$$;

REVOKE ALL ON FUNCTION public.process_cumulative_observation(UUID, TEXT, NUMERIC, TIMESTAMPTZ, TEXT)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.process_cumulative_observation(UUID, TEXT, NUMERIC, TIMESTAMPTZ, TEXT)
  TO service_role;
