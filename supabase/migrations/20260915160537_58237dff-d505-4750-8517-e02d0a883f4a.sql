-- Baselines belong to watchlist entries, so removal/re-addition starts afresh.
CREATE TABLE public.monitor_baselines (
  user_id UUID NOT NULL,
  symbol TEXT NOT NULL,
  baseline_price NUMERIC NOT NULL CHECK (baseline_price > 0 AND baseline_price < 'Infinity'::numeric),
  baseline_at TIMESTAMPTZ NOT NULL,
  data_source TEXT NOT NULL,
  threshold_pct NUMERIC NOT NULL,
  last_observed_at TIMESTAMPTZ NOT NULL,
  last_up_alert_at TIMESTAMPTZ,
  last_down_alert_at TIMESTAMPTZ,
  PRIMARY KEY (user_id, symbol),
  FOREIGN KEY (user_id, symbol) REFERENCES public.watchlist_items(user_id, symbol) ON DELETE CASCADE
);
ALTER TABLE public.monitor_baselines ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.monitor_baselines TO authenticated;
GRANT ALL ON public.monitor_baselines TO service_role;
CREATE POLICY "own baselines" ON public.monitor_baselines FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

-- Old alerts retain their actual rolling windows. New alerts identify their baseline.
ALTER TABLE public.alerts ALTER COLUMN window_minutes DROP NOT NULL;
ALTER TABLE public.alerts
  ADD COLUMN comparison_mode TEXT NOT NULL DEFAULT 'rolling',
  ADD COLUMN baseline_price NUMERIC,
  ADD COLUMN baseline_at TIMESTAMPTZ,
  ADD COLUMN observed_at TIMESTAMPTZ;

-- Trusted server only. One transaction locks state, evaluates, writes the alert,
-- and advances the baseline. A failed INSERT rolls back the entire transition.
CREATE FUNCTION public.process_cumulative_observation(
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

  -- Serialize checks even before the first baseline exists, and protect deletion.
  PERFORM 1 FROM public.watchlist_items
    WHERE user_id = p_user_id AND symbol = p_symbol FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('status', 'not_watched'); END IF;

  SELECT * INTO cfg FROM public.monitor_settings WHERE user_id = p_user_id FOR SHARE;
  IF FOUND THEN
    IF NOT cfg.monitoring_enabled THEN RETURN jsonb_build_object('status', 'disabled'); END IF;
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
  -- Reject out-of-order runs and repeated observations, including across providers.
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
  -- Cross-multiply for exact threshold boundaries; percentage is for display/storage.
  IF abs(p_price - s.baseline_price) * 100 < s.baseline_price * threshold THEN
    RETURN jsonb_build_object('status', 'below_threshold', 'change_pct', changed);
  END IF;
  direction := CASE WHEN p_price > s.baseline_price THEN 'up' ELSE 'down' END;
  last_alert := CASE WHEN direction = 'up' THEN s.last_up_alert_at ELSE s.last_down_alert_at END;
  IF last_alert IS NOT NULL AND run_at < last_alert + make_interval(mins => cooldown) THEN
    -- Keep the baseline; continued movement is eligible on a fresh check after cooldown.
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