-- Current operational schema for a fresh external PostgreSQL/Supabase database.
-- Disposable pre-release working state: recreate/reset from this single baseline.
-- Historical operational schema evolution is intentionally not preserved yet.
-- Once production data must survive upgrades, freeze this baseline and use forward migrations.
-- Never apply this file to the Lovable/application database.
-- Deployment supplies the public schema and anon/authenticated/service_role roles.
-- Browsers and Python have no operational table access; backend services use service_role.

-- Tables: operational store, shared collector, and movement episode persistence.
CREATE TABLE public.recent_candles (
  user_id UUID NOT NULL,
  instrument_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  native_symbol TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('binance-usdm', 'okx-usdt-swap', 'kraken-futures')),
  endpoint TEXT NOT NULL,
  market_type TEXT NOT NULL DEFAULT 'futures' CHECK (market_type = 'futures'),
  contract_type TEXT NOT NULL DEFAULT 'perpetual' CHECK (contract_type = 'perpetual'),
  price_type TEXT NOT NULL CHECK (price_type = 'trade'),
  timeframe_minutes INTEGER NOT NULL CHECK (timeframe_minutes IN (1, 15, 60, 240)),
  open_time TIMESTAMPTZ NOT NULL,
  close_time TIMESTAMPTZ NOT NULL,
  open DOUBLE PRECISION NOT NULL CHECK (open > 0 AND open < 'Infinity'::float8),
  high DOUBLE PRECISION NOT NULL CHECK (high > 0 AND high < 'Infinity'::float8),
  low DOUBLE PRECISION NOT NULL CHECK (low > 0 AND low < 'Infinity'::float8),
  close DOUBLE PRECISION NOT NULL CHECK (close > 0 AND close < 'Infinity'::float8),
  volume DOUBLE PRECISION NOT NULL CHECK (volume >= 0 AND volume < 'Infinity'::float8),
  source_event_at TIMESTAMPTZ NOT NULL,
  collected_at TIMESTAMPTZ NOT NULL,
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id, instrument_id, timeframe_minutes, open_time),
  CHECK (high >= greatest(open, close) AND low <= least(open, close) AND high >= low),
  CHECK (close_time = open_time + make_interval(mins => timeframe_minutes)),
  CHECK (source_event_at >= close_time),
  CHECK (collected_at >= source_event_at),
  CHECK (mod(extract(epoch FROM open_time)::BIGINT, timeframe_minutes * 60) = 0),
  CHECK (instrument_id = source || ':' || native_symbol)
);

CREATE TABLE public.market_data_checkpoints (
  user_id UUID NOT NULL,
  instrument_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  native_symbol TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('binance-usdm', 'okx-usdt-swap', 'kraken-futures')),
  endpoint TEXT NOT NULL,
  market_type TEXT NOT NULL DEFAULT 'futures' CHECK (market_type = 'futures'),
  contract_type TEXT NOT NULL DEFAULT 'perpetual' CHECK (contract_type = 'perpetual'),
  price_type TEXT NOT NULL CHECK (price_type = 'trade'),
  timeframe_minutes INTEGER NOT NULL CHECK (timeframe_minutes IN (1, 15, 60, 240)),
  observed_at TIMESTAMPTZ NOT NULL,
  price DOUBLE PRECISION NOT NULL CHECK (price > 0 AND price < 'Infinity'::float8),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id, instrument_id, timeframe_minutes),
  CHECK (instrument_id = source || ':' || native_symbol)
);

CREATE TABLE public.monitor_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL,
  ran_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  status TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
  symbols_checked INTEGER NOT NULL CHECK (symbols_checked >= 0),
  alerts_created INTEGER NOT NULL CHECK (alerts_created >= 0),
  data_source TEXT CHECK (
    data_source IS NULL OR data_source IN ('binance-usdm', 'okx-usdt-swap', 'kraken-futures')
  ),
  error_message TEXT,
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metrics) = 'object')
);

-- Dormant infrastructure for a future external-owned durable result. No current
-- application domain writes here, avoiding a duplicate of Lovable history.
CREATE TABLE public.operational_results (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL,
  result_kind TEXT NOT NULL CHECK (result_kind ~ '^[a-z][a-z0-9_.-]{0,63}$'),
  payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (id, user_id, result_kind)
);

CREATE TABLE public.sync_outbox (
  event_id UUID PRIMARY KEY,
  result_id UUID NOT NULL REFERENCES public.operational_results(id) ON DELETE RESTRICT,
  user_id UUID NOT NULL,
  result_kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'failed', 'dead', 'delivered')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  locked_by UUID,
  locked_until TIMESTAMPTZ,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  delivered_at TIMESTAMPTZ,
  FOREIGN KEY (result_id, user_id, result_kind)
    REFERENCES public.operational_results(id, user_id, result_kind) ON DELETE RESTRICT,
  CHECK ((status = 'delivered') = (delivered_at IS NOT NULL)),
  CHECK ((status = 'processing') = (locked_by IS NOT NULL AND locked_until IS NOT NULL))
);

CREATE TABLE public.collector_recent_candles (
  instrument_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  native_symbol TEXT NOT NULL,
  provider TEXT NOT NULL CHECK (provider = 'binance-usdm'),
  endpoint TEXT NOT NULL CHECK (endpoint = '/fapi/v1/klines' OR endpoint = 'wss://fstream.binance.com/market/stream'),
  market_type TEXT NOT NULL DEFAULT 'futures' CHECK (market_type = 'futures'),
  contract_type TEXT NOT NULL DEFAULT 'perpetual' CHECK (contract_type = 'perpetual'),
  price_type TEXT NOT NULL CHECK (price_type = 'trade'),
  timeframe_minutes INTEGER NOT NULL CHECK (timeframe_minutes IN (1, 15, 60, 240)),
  open_time TIMESTAMPTZ NOT NULL,
  close_time TIMESTAMPTZ NOT NULL,
  open DOUBLE PRECISION NOT NULL CHECK (open > 0 AND open < 'Infinity'::float8),
  high DOUBLE PRECISION NOT NULL CHECK (high > 0 AND high < 'Infinity'::float8),
  low DOUBLE PRECISION NOT NULL CHECK (low > 0 AND low < 'Infinity'::float8),
  close DOUBLE PRECISION NOT NULL CHECK (close > 0 AND close < 'Infinity'::float8),
  volume DOUBLE PRECISION NOT NULL CHECK (volume >= 0 AND volume < 'Infinity'::float8),
  quote_volume DOUBLE PRECISION NOT NULL CHECK (quote_volume >= 0 AND quote_volume < 'Infinity'::float8),
  source_event_at TIMESTAMPTZ,
  received_at TIMESTAMPTZ NOT NULL,
  transport TEXT NOT NULL CHECK (transport IN ('rest', 'websocket')),
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (provider, instrument_id, price_type, timeframe_minutes, open_time),
  CONSTRAINT collector_candle_provenance CHECK (
    (transport = 'rest' AND endpoint = '/fapi/v1/klines' AND source_event_at IS NULL)
    OR (transport = 'websocket' AND endpoint = 'wss://fstream.binance.com/market/stream'
        AND source_event_at IS NOT NULL)
  ),
  CHECK (instrument_id = provider || ':' || native_symbol),
  CHECK (high >= greatest(open, close) AND low <= least(open, close) AND high >= low),
  CHECK (close_time = open_time + make_interval(mins => timeframe_minutes) - interval '1 millisecond'),
  CHECK (mod(extract(epoch FROM open_time)::BIGINT, timeframe_minutes * 60) = 0)
);

CREATE TABLE public.collector_health (
  instrument_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe_minutes INTEGER NOT NULL CHECK (timeframe_minutes IN (1, 15, 60, 240)),
  status TEXT NOT NULL CHECK (status IN ('LIVE', 'RECOVERING', 'STALE', 'UNAVAILABLE')),
  last_event_at TIMESTAMPTZ,
  last_completed_open_time TIMESTAMPTZ,
  lag_ms BIGINT CHECK (lag_ms IS NULL OR lag_ms >= 0),
  queue_depth INTEGER NOT NULL DEFAULT 0 CHECK (queue_depth >= 0),
  reconnect_count INTEGER NOT NULL DEFAULT 0 CHECK (reconnect_count >= 0),
  error_message TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (instrument_id, timeframe_minutes),
  CHECK (instrument_id = 'binance-usdm:' || symbol)
);

-- Derived collector input. The application/orchestration side owns user
-- watchlists and assigns this shared symbol set; the worker reads it without
-- Lovable credentials. This is never a second watchlist authority.
CREATE TABLE public.collector_subscriptions (
  universe_name TEXT PRIMARY KEY CHECK (universe_name = 'binance-usdm-shared'),
  symbols TEXT[] NOT NULL,
  assigned_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE public.collector_leases (
  lease_name TEXT PRIMARY KEY CHECK (lease_name = 'binance-usdm-public-market'),
  instance_id UUID NOT NULL,
  leased_until TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE public.market_state_current (
  universe_id TEXT NOT NULL,
  primary_window_minutes INTEGER NOT NULL CHECK (primary_window_minutes = 5),
  universe_version TEXT NOT NULL,
  provider TEXT NOT NULL CHECK (provider = 'binance-usdm'),
  exchange TEXT NOT NULL CHECK (exchange = 'binance'),
  price_type TEXT NOT NULL CHECK (price_type = 'trade'),
  evaluation_boundary_time TIMESTAMPTZ NOT NULL,
  direction_state TEXT NOT NULL CHECK (direction_state IN ('BROAD_RISE', 'BROAD_DROP', 'NEUTRAL', 'WARMING', 'UNAVAILABLE')),
  pace TEXT NOT NULL CHECK (pace IN ('ACCELERATING', 'DECELERATING', 'MIXED', 'NOT_APPLICABLE')),
  active_episode_id TEXT,
  active_direction TEXT CHECK (active_direction IS NULL OR active_direction IN ('BROAD_RISE', 'BROAD_DROP')),
  interrupted BOOLEAN NOT NULL DEFAULT false,
  episode_algorithm_version TEXT NOT NULL,
  lifecycle_config_version TEXT NOT NULL,
  classifier_algorithm_version TEXT NOT NULL,
  classifier_config_version TEXT NOT NULL,
  movement_algorithm_version TEXT NOT NULL,
  movement_config_version TEXT NOT NULL,
  lifecycle_state JSONB NOT NULL,
  current_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (universe_id, primary_window_minutes)
);

CREATE TABLE public.market_movement_events (
  event_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  previous_episode_id TEXT,
  episode_algorithm_version TEXT NOT NULL,
  lifecycle_config_version TEXT NOT NULL,
  transition TEXT NOT NULL CHECK (transition IN ('STARTED', 'STRENGTHENED', 'WEAKENED', 'REVERSED', 'ENDED')),
  transition_reason TEXT NOT NULL,
  from_direction TEXT CHECK (from_direction IS NULL OR from_direction IN ('BROAD_RISE', 'BROAD_DROP')),
  to_direction TEXT CHECK (to_direction IS NULL OR to_direction IN ('BROAD_RISE', 'BROAD_DROP')),
  episode_start_boundary_time TIMESTAMPTZ NOT NULL,
  evaluation_boundary_time TIMESTAMPTZ NOT NULL,
  universe_id TEXT NOT NULL,
  universe_version TEXT NOT NULL,
  primary_window_minutes INTEGER NOT NULL CHECK (primary_window_minutes = 5),
  provider TEXT NOT NULL CHECK (provider = 'binance-usdm'),
  exchange TEXT NOT NULL CHECK (exchange = 'binance'),
  price_type TEXT NOT NULL CHECK (price_type = 'trade'),
  direction TEXT NOT NULL CHECK (direction IN ('BROAD_RISE', 'BROAD_DROP')),
  pace TEXT NOT NULL CHECK (pace IN ('ACCELERATING', 'DECELERATING', 'MIXED', 'NOT_APPLICABLE')),
  directional_breadth DOUBLE PRECISION,
  material_breadth DOUBLE PRECISION,
  median_raw_return DOUBLE PRECISION,
  median_normalized_movement DOUBLE PRECISION,
  median_acceleration DOUBLE PRECISION,
  acceleration_breadth DOUBLE PRECISION,
  dispersion DOUBLE PRECISION,
  rvol_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  outliers JSONB NOT NULL DEFAULT '[]'::jsonb,
  supporting_contracts JSONB NOT NULL DEFAULT '[]'::jsonb,
  conflicting_contracts JSONB NOT NULL DEFAULT '[]'::jsonb,
  configured_universe JSONB NOT NULL DEFAULT '[]'::jsonb,
  included_symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
  excluded_symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
  windows_context JSONB NOT NULL DEFAULT '[]'::jsonb,
  classifier_algorithm_version TEXT NOT NULL,
  classifier_config_version TEXT NOT NULL,
  movement_algorithm_version TEXT NOT NULL,
  movement_config_version TEXT NOT NULL,
  canonical_event JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- Indexes.
CREATE INDEX recent_candles_user_time
  ON public.recent_candles(user_id, open_time DESC);

CREATE INDEX monitor_runs_user_time ON public.monitor_runs(user_id, ran_at DESC);

CREATE INDEX sync_outbox_due
  ON public.sync_outbox(status, available_at, created_at)
  WHERE status IN ('pending', 'failed', 'processing');

CREATE INDEX sync_outbox_user ON public.sync_outbox(user_id, created_at DESC);

-- A completed candle finalized from the live stream can differ from the REST candle for the same
-- stable identity (for example after a disconnect, when trades were missed). The stored candle is
-- never overwritten, and one conflicting row must not fail the whole batch (that took every symbol
-- offline). The conflicting INCOMING candle is quarantined for inspection and the rest of the batch
-- is written. The quarantine is a bounded diagnostic log, not a data source.
CREATE TABLE public.collector_candle_quarantine (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider TEXT NOT NULL,
  instrument_id TEXT NOT NULL,
  price_type TEXT NOT NULL,
  timeframe_minutes INTEGER NOT NULL,
  open_time TIMESTAMPTZ NOT NULL,
  stored JSONB NOT NULL,
  incoming JSONB NOT NULL,
  incoming_hash TEXT NOT NULL,
  quarantined_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  -- A retried recovery re-submits the same conflicting candle; keep one row per distinct candle.
  UNIQUE (provider, instrument_id, price_type, timeframe_minutes, open_time, incoming_hash)
);
CREATE INDEX collector_candle_quarantine_time
  ON public.collector_candle_quarantine(quarantined_at DESC);

CREATE INDEX collector_recent_candles_time
  ON public.collector_recent_candles(open_time DESC);

CREATE INDEX market_movement_events_episode
  ON public.market_movement_events(episode_id, evaluation_boundary_time);

CREATE INDEX market_movement_events_boundary
  ON public.market_movement_events(evaluation_boundary_time DESC);

-- Row-level security and table access.
ALTER TABLE public.recent_candles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.market_data_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monitor_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.operational_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collector_recent_candles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collector_candle_quarantine ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collector_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collector_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collector_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.market_state_current ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.market_movement_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, anon, authenticated;
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;

-- Functions/RPCs: each final definition appears once, after its table dependencies.
CREATE FUNCTION public.record_recent_candles(
  p_user_id UUID,
  p_rows JSONB,
  p_retention_days INTEGER DEFAULT 7
) RETURNS INTEGER
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  changed_rows INTEGER;
BEGIN
  IF p_user_id IS NULL OR jsonb_typeof(p_rows) <> 'array'
     OR jsonb_array_length(p_rows) < 1 OR jsonb_array_length(p_rows) > 1000
     OR p_retention_days < 1 OR p_retention_days > 30 THEN
    RAISE EXCEPTION 'Invalid recent-candle batch';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM jsonb_to_recordset(p_rows) AS x(
      instrument_id TEXT, timeframe_minutes INTEGER, open_time TIMESTAMPTZ,
      open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
      close DOUBLE PRECISION, volume DOUBLE PRECISION, source_event_at TIMESTAMPTZ
    )
    JOIN public.recent_candles c
      ON c.user_id = p_user_id AND c.instrument_id = x.instrument_id
      AND c.timeframe_minutes = x.timeframe_minutes AND c.open_time = x.open_time
    WHERE c.open <> x.open OR c.high <> x.high OR c.low <> x.low OR c.close <> x.close
      OR c.volume <> x.volume OR c.source_event_at <> x.source_event_at
  ) THEN
    RAISE EXCEPTION 'Conflicting completed candle for stable identity';
  END IF;

  INSERT INTO public.recent_candles (
    user_id, instrument_id, symbol, native_symbol, source, endpoint, price_type,
    timeframe_minutes, open_time, close_time, open, high, low, close, volume,
    source_event_at, collected_at
  )
  SELECT p_user_id, x.instrument_id, x.symbol, x.native_symbol, x.source, x.endpoint,
    x.price_type, x.timeframe_minutes, x.open_time, x.close_time, x.open, x.high,
    x.low, x.close, x.volume, x.source_event_at, x.collected_at
  FROM jsonb_to_recordset(p_rows) AS x(
    instrument_id TEXT, symbol TEXT, native_symbol TEXT, source TEXT, endpoint TEXT,
    price_type TEXT, timeframe_minutes INTEGER, open_time TIMESTAMPTZ,
    close_time TIMESTAMPTZ, open DOUBLE PRECISION, high DOUBLE PRECISION,
    low DOUBLE PRECISION, close DOUBLE PRECISION, volume DOUBLE PRECISION,
    source_event_at TIMESTAMPTZ, collected_at TIMESTAMPTZ
  )
  ON CONFLICT (user_id, instrument_id, timeframe_minutes, open_time) DO UPDATE
    SET collected_at = greatest(public.recent_candles.collected_at, EXCLUDED.collected_at),
        endpoint = EXCLUDED.endpoint
    WHERE public.recent_candles.open = EXCLUDED.open
      AND public.recent_candles.high = EXCLUDED.high
      AND public.recent_candles.low = EXCLUDED.low
      AND public.recent_candles.close = EXCLUDED.close
      AND public.recent_candles.volume = EXCLUDED.volume
      AND public.recent_candles.source_event_at = EXCLUDED.source_event_at;
  GET DIAGNOSTICS changed_rows = ROW_COUNT;

  DELETE FROM public.recent_candles
    WHERE close_time < clock_timestamp() - make_interval(days => p_retention_days);
  DELETE FROM public.market_data_checkpoints
    WHERE updated_at < clock_timestamp() - interval '30 days';
  RETURN changed_rows;
END;
$$;

CREATE FUNCTION public.record_market_data_checkpoint(
  p_user_id UUID, p_instrument_id TEXT, p_symbol TEXT, p_native_symbol TEXT,
  p_source TEXT, p_endpoint TEXT, p_price_type TEXT, p_timeframe_minutes INTEGER,
  p_observed_at TIMESTAMPTZ, p_price DOUBLE PRECISION
) RETURNS JSONB
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  changed_rows INTEGER;
BEGIN
  IF p_observed_at IS NULL OR p_observed_at > clock_timestamp()
     OR p_observed_at < clock_timestamp() - interval '10 minutes'
     OR date_trunc('minute', p_observed_at) <> p_observed_at THEN
    RAISE EXCEPTION 'Invalid or stale checkpoint';
  END IF;
  INSERT INTO public.market_data_checkpoints (
    user_id, instrument_id, symbol, native_symbol, source, endpoint, price_type,
    timeframe_minutes, observed_at, price, updated_at
  ) VALUES (
    p_user_id, p_instrument_id, p_symbol, p_native_symbol, p_source, p_endpoint,
    p_price_type, p_timeframe_minutes, p_observed_at, p_price, clock_timestamp()
  )
  ON CONFLICT (user_id, instrument_id, timeframe_minutes) DO UPDATE
    SET symbol = EXCLUDED.symbol,
        native_symbol = EXCLUDED.native_symbol,
        source = EXCLUDED.source,
        endpoint = EXCLUDED.endpoint,
        price_type = EXCLUDED.price_type,
        observed_at = EXCLUDED.observed_at,
        price = EXCLUDED.price,
        updated_at = clock_timestamp()
    WHERE public.market_data_checkpoints.observed_at < EXCLUDED.observed_at;
  GET DIAGNOSTICS changed_rows = ROW_COUNT;
  RETURN jsonb_build_object(
    'status', CASE WHEN changed_rows = 0 THEN 'already_processed' ELSE 'recorded' END
  );
END;
$$;

CREATE FUNCTION public.record_monitor_run(
  p_user_id UUID, p_status TEXT, p_symbols_checked INTEGER, p_alerts_created INTEGER,
  p_data_source TEXT, p_error_message TEXT, p_duration_ms INTEGER, p_metrics JSONB,
  p_retention_days INTEGER DEFAULT 30
) RETURNS UUID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE saved_id UUID;
BEGIN
  IF p_retention_days < 1 OR p_retention_days > 90 THEN
    RAISE EXCEPTION 'Invalid monitor-run retention';
  END IF;
  INSERT INTO public.monitor_runs (
    user_id, status, symbols_checked, alerts_created, data_source, error_message,
    duration_ms, metrics
  ) VALUES (
    p_user_id, p_status, p_symbols_checked, p_alerts_created, p_data_source,
    p_error_message, p_duration_ms, p_metrics
  ) RETURNING id INTO saved_id;
  DELETE FROM public.monitor_runs
    WHERE ran_at < clock_timestamp() - make_interval(days => p_retention_days);
  RETURN saved_id;
END;
$$;

CREATE FUNCTION public.stage_durable_result(
  p_result_id UUID, p_event_id UUID, p_user_id UUID, p_result_kind TEXT, p_payload JSONB
) RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_result_id IS NULL OR p_event_id IS NULL OR p_user_id IS NULL
     OR p_result_kind !~ '^[a-z][a-z0-9_.-]{0,63}$'
     OR jsonb_typeof(p_payload) <> 'object' OR pg_column_size(p_payload) > 1048576 THEN
    RAISE EXCEPTION 'Invalid durable result';
  END IF;
  INSERT INTO public.operational_results(id, user_id, result_kind, payload)
    VALUES (p_result_id, p_user_id, p_result_kind, p_payload)
    ON CONFLICT (id) DO NOTHING;
  IF NOT EXISTS (
    SELECT 1 FROM public.operational_results
    WHERE id = p_result_id AND user_id = p_user_id
      AND result_kind = p_result_kind AND payload = p_payload
  ) THEN RAISE EXCEPTION 'Stable result ID conflict'; END IF;

  INSERT INTO public.sync_outbox(event_id, result_id, user_id, result_kind)
    VALUES (p_event_id, p_result_id, p_user_id, p_result_kind)
    ON CONFLICT (event_id) DO NOTHING;
  IF NOT EXISTS (
    SELECT 1 FROM public.sync_outbox
    WHERE event_id = p_event_id AND result_id = p_result_id
      AND user_id = p_user_id AND result_kind = p_result_kind
  ) THEN RAISE EXCEPTION 'Stable event ID conflict'; END IF;
END;
$$;

CREATE FUNCTION public.claim_sync_outbox(p_worker_id UUID, p_limit INTEGER DEFAULT 50)
RETURNS TABLE (
  event_id UUID, result_id UUID, user_id UUID, result_kind TEXT, payload JSONB, attempts INTEGER
)
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_worker_id IS NULL OR p_limit < 1 OR p_limit > 100 THEN
    RAISE EXCEPTION 'Invalid outbox claim';
  END IF;
  RETURN QUERY
  WITH candidates AS (
    SELECT o.event_id
    FROM public.sync_outbox o
    WHERE (
      o.status IN ('pending', 'failed') AND o.available_at <= clock_timestamp()
    ) OR (
      o.status = 'processing' AND o.locked_until < clock_timestamp()
    )
    ORDER BY o.created_at
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  ), claimed AS (
    UPDATE public.sync_outbox o
      SET status = 'processing', attempts = o.attempts + 1,
          locked_by = p_worker_id, locked_until = clock_timestamp() + interval '5 minutes',
          last_error = NULL
    FROM candidates c
    WHERE o.event_id = c.event_id
    RETURNING o.event_id, o.result_id, o.user_id, o.result_kind, o.attempts
  )
  SELECT c.event_id, c.result_id, c.user_id, c.result_kind, r.payload, c.attempts
  FROM claimed c JOIN public.operational_results r ON r.id = c.result_id;
END;
$$;

CREATE FUNCTION public.mark_sync_outbox_delivered(p_event_id UUID, p_worker_id UUID)
RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  UPDATE public.sync_outbox SET status = 'delivered', delivered_at = clock_timestamp(),
    locked_by = NULL, locked_until = NULL, last_error = NULL
  WHERE event_id = p_event_id AND status = 'processing' AND locked_by = p_worker_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'Outbox delivery lease is not owned'; END IF;
END;
$$;

CREATE FUNCTION public.mark_sync_outbox_failed(
  p_event_id UUID, p_worker_id UUID, p_error TEXT, p_max_attempts INTEGER DEFAULT 10
) RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_error IS NULL OR p_error = '' OR p_max_attempts < 1 OR p_max_attempts > 100 THEN
    RAISE EXCEPTION 'Invalid outbox failure';
  END IF;
  UPDATE public.sync_outbox
    SET status = CASE WHEN attempts >= p_max_attempts THEN 'dead' ELSE 'failed' END,
        available_at = clock_timestamp()
          + make_interval(secs => least(3600, (power(2, least(attempts, 10)))::INTEGER)),
        locked_by = NULL, locked_until = NULL, last_error = left(p_error, 2000)
  WHERE event_id = p_event_id AND status = 'processing' AND locked_by = p_worker_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'Outbox failure lease is not owned'; END IF;
END;
$$;

CREATE FUNCTION public.get_storage_diagnostics(p_user_id UUID)
RETURNS TABLE (
  recent_candle_rows BIGINT, checkpoint_rows BIGINT, monitor_run_rows BIGINT,
  pending_outbox_rows BIGINT, failed_outbox_rows BIGINT, dead_outbox_rows BIGINT,
  oldest_candle_at TIMESTAMPTZ, oldest_undelivered_at TIMESTAMPTZ
)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  SELECT
    (SELECT count(*) FROM public.recent_candles WHERE user_id = p_user_id),
    (SELECT count(*) FROM public.market_data_checkpoints WHERE user_id = p_user_id),
    (SELECT count(*) FROM public.monitor_runs WHERE user_id = p_user_id),
    (SELECT count(*) FROM public.sync_outbox WHERE user_id = p_user_id AND status IN ('pending','processing')),
    (SELECT count(*) FROM public.sync_outbox WHERE user_id = p_user_id AND status = 'failed'),
    (SELECT count(*) FROM public.sync_outbox WHERE user_id = p_user_id AND status = 'dead'),
    (SELECT min(open_time) FROM public.recent_candles WHERE user_id = p_user_id),
    (SELECT min(created_at) FROM public.sync_outbox WHERE user_id = p_user_id AND status <> 'delivered');
$$;

CREATE FUNCTION public.get_global_storage_diagnostics()
RETURNS TABLE (
  recent_candle_rows BIGINT, checkpoint_rows BIGINT, monitor_run_rows BIGINT,
  pending_outbox_rows BIGINT, failed_outbox_rows BIGINT, dead_outbox_rows BIGINT,
  oldest_candle_at TIMESTAMPTZ, oldest_undelivered_at TIMESTAMPTZ
)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  SELECT
    (SELECT count(*) FROM public.recent_candles),
    (SELECT count(*) FROM public.market_data_checkpoints),
    (SELECT count(*) FROM public.monitor_runs),
    (SELECT count(*) FROM public.sync_outbox WHERE status IN ('pending','processing')),
    (SELECT count(*) FROM public.sync_outbox WHERE status = 'failed'),
    (SELECT count(*) FROM public.sync_outbox WHERE status = 'dead'),
    (SELECT min(open_time) FROM public.recent_candles),
    (SELECT min(created_at) FROM public.sync_outbox WHERE status <> 'delivered');
$$;

-- Only delivered outbox state is eligible for cleanup. A result remains while any
-- outbox row references it, so pending/failed/dead durable state cannot be purged.
CREATE FUNCTION public.purge_delivered_results(p_retention_days INTEGER DEFAULT 30)
RETURNS INTEGER
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE deleted_rows INTEGER;
BEGIN
  IF p_retention_days < 1 OR p_retention_days > 365 THEN
    RAISE EXCEPTION 'Invalid delivered-result retention';
  END IF;
  DELETE FROM public.sync_outbox
    WHERE status = 'delivered'
      AND delivered_at < clock_timestamp() - make_interval(days => p_retention_days);
  DELETE FROM public.operational_results r
    WHERE NOT EXISTS (SELECT 1 FROM public.sync_outbox o WHERE o.result_id = r.id);
  GET DIAGNOSTICS deleted_rows = ROW_COUNT;
  RETURN deleted_rows;
END;
$$;

CREATE FUNCTION public.assign_collector_subscriptions(p_symbols TEXT[])
RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  normalized TEXT[];
BEGIN
  IF p_symbols IS NULL THEN
    RAISE EXCEPTION 'Invalid collector subscription set';
  END IF;
  SELECT coalesce(
      array_agg(DISTINCT upper(btrim(value)) ORDER BY upper(btrim(value))),
      ARRAY[]::TEXT[]
    )
    INTO normalized
  FROM unnest(p_symbols) AS value
  WHERE btrim(value) <> '';
  IF array_length(normalized, 1) > 100 THEN
    RAISE EXCEPTION 'Collector subscription set exceeds the supported maximum';
  END IF;
  INSERT INTO public.collector_subscriptions(universe_name, symbols, assigned_at)
    VALUES ('binance-usdm-shared', normalized, clock_timestamp())
  ON CONFLICT (universe_name) DO UPDATE SET
    symbols = EXCLUDED.symbols,
    assigned_at = clock_timestamp();

  UPDATE public.collector_health SET
    status = 'UNAVAILABLE',
    error_message = 'collection disabled/unsubscribed',
    lag_ms = NULL,
    queue_depth = 0,
    updated_at = clock_timestamp()
  WHERE NOT (symbol = ANY(normalized));
END;
$$;

-- Day-based retention bounds the bulk of the store while each canonical series
-- retains its newest 260 completed candles for TA minimum history and catch-up.
-- One-minute candles keep p_minute_retention_days (default 62, bounded 31..90): the
-- forward engine (#239) needs 28 days of seasonal profile plus the EWMA warm-up and
-- 30 days of signal history per symbol.
CREATE FUNCTION public.record_collector_candles(
  p_rows JSONB, p_retention_days INTEGER DEFAULT 7, p_minute_retention_days INTEGER DEFAULT 62
) RETURNS TABLE (candle_identity TEXT)
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF jsonb_typeof(p_rows) <> 'array' OR jsonb_array_length(p_rows) < 1
     OR jsonb_array_length(p_rows) > 1000
     OR p_retention_days < 1 OR p_retention_days > 30
     OR p_minute_retention_days < 31 OR p_minute_retention_days > 90 THEN
    RAISE EXCEPTION 'Invalid collector candle batch';
  END IF;

  RETURN QUERY
  WITH incoming AS (
    SELECT x.*, to_jsonb(x) AS incoming_json
    FROM jsonb_to_recordset(p_rows) AS x(
      instrument_id TEXT, symbol TEXT, native_symbol TEXT, provider TEXT, endpoint TEXT,
      price_type TEXT, timeframe_minutes INTEGER, open_time TIMESTAMPTZ,
      close_time TIMESTAMPTZ, open DOUBLE PRECISION, high DOUBLE PRECISION,
      low DOUBLE PRECISION, close DOUBLE PRECISION, volume DOUBLE PRECISION,
      quote_volume DOUBLE PRECISION, source_event_at TIMESTAMPTZ,
      received_at TIMESTAMPTZ, transport TEXT
    )
  ),
  conflicting AS (
    SELECT i.*, to_jsonb(c) - 'inserted_at' AS stored_json
    FROM incoming i
    JOIN public.collector_recent_candles c USING (
      provider, instrument_id, price_type, timeframe_minutes, open_time
    )
    WHERE c.close_time <> i.close_time OR c.open <> i.open OR c.high <> i.high
      OR c.low <> i.low OR c.close <> i.close OR c.volume <> i.volume
      OR c.quote_volume IS DISTINCT FROM i.quote_volume
  ),
  quarantined AS (
    INSERT INTO public.collector_candle_quarantine (
      provider, instrument_id, price_type, timeframe_minutes, open_time,
      stored, incoming, incoming_hash
    )
    SELECT k.provider, k.instrument_id, k.price_type, k.timeframe_minutes, k.open_time,
      k.stored_json, k.incoming_json, md5(k.incoming_json::TEXT)
    FROM conflicting k
    ON CONFLICT DO NOTHING
    RETURNING 1
  ),
  inserted AS (
    INSERT INTO public.collector_recent_candles (
      instrument_id, symbol, native_symbol, provider, endpoint, price_type,
      timeframe_minutes, open_time, close_time, open, high, low, close, volume,
      quote_volume, source_event_at, received_at, transport
    )
    SELECT i.instrument_id, i.symbol, i.native_symbol, i.provider, i.endpoint,
      i.price_type, i.timeframe_minutes, i.open_time, i.close_time, i.open, i.high,
      i.low, i.close, i.volume, i.quote_volume, i.source_event_at, i.received_at,
      i.transport
    FROM incoming i
    WHERE NOT EXISTS (
      SELECT 1 FROM conflicting k
      WHERE k.provider = i.provider AND k.instrument_id = i.instrument_id
        AND k.price_type = i.price_type AND k.timeframe_minutes = i.timeframe_minutes
        AND k.open_time = i.open_time
    )
    ON CONFLICT (provider, instrument_id, price_type, timeframe_minutes, open_time)
      DO NOTHING
    RETURNING provider, instrument_id, price_type, timeframe_minutes, open_time
  )
  SELECT provider || '|' || instrument_id || '|' || price_type || '|'
    || timeframe_minutes::TEXT || '|' || (extract(epoch FROM open_time) * 1000)::BIGINT::TEXT
  FROM inserted;

  DELETE FROM public.collector_candle_quarantine
    WHERE quarantined_at < clock_timestamp() - INTERVAL '30 days';

  -- Keep the newest 260 completed candles per canonical series (TA history and catch-up margin).
  DELETE FROM public.collector_recent_candles c
    WHERE c.close_time < clock_timestamp() - make_interval(days => CASE
            WHEN c.timeframe_minutes = 1 THEN p_minute_retention_days ELSE p_retention_days END)
      AND c.open_time < (
        SELECT min(keep.open_time)
        FROM (
          SELECT k.open_time
          FROM public.collector_recent_candles k
          WHERE k.provider = c.provider
            AND k.instrument_id = c.instrument_id
            AND k.price_type = c.price_type
            AND k.timeframe_minutes = c.timeframe_minutes
          ORDER BY k.open_time DESC
          LIMIT 260
        ) keep
      );
END;
$$;

CREATE FUNCTION public.get_collector_subscriptions()
RETURNS TEXT[]
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  SELECT coalesce(
    (SELECT symbols FROM public.collector_subscriptions
      WHERE universe_name = 'binance-usdm-shared'),
    ARRAY[]::TEXT[]
  );
$$;

CREATE FUNCTION public.record_collector_health(
  p_instrument_id TEXT, p_symbol TEXT, p_timeframe_minutes INTEGER, p_status TEXT,
  p_last_event_at TIMESTAMPTZ, p_last_completed_open_time TIMESTAMPTZ,
  p_lag_ms BIGINT, p_queue_depth INTEGER, p_reconnect_count INTEGER,
  p_error_message TEXT
) RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  subscribed BOOLEAN;
BEGIN
  -- SHARE conflicts with the ROW EXCLUSIVE lock held by assignment's upsert.
  -- Hold it through the health write, including when no assignment row exists,
  -- so assignment/invalidation cannot race with a previously queued intent.
  LOCK TABLE public.collector_subscriptions IN SHARE MODE;
  SELECT EXISTS (
    SELECT 1 FROM public.collector_subscriptions
    WHERE universe_name = 'binance-usdm-shared' AND p_symbol = ANY(symbols)
  ) INTO subscribed;

  INSERT INTO public.collector_health (
    instrument_id, symbol, timeframe_minutes, status, last_event_at,
    last_completed_open_time, lag_ms, queue_depth, reconnect_count, error_message
  ) VALUES (
    p_instrument_id, p_symbol, p_timeframe_minutes,
    CASE WHEN subscribed THEN p_status ELSE 'UNAVAILABLE' END,
    p_last_event_at, p_last_completed_open_time,
    CASE WHEN subscribed THEN p_lag_ms ELSE NULL END,
    CASE WHEN subscribed THEN p_queue_depth ELSE 0 END,
    p_reconnect_count,
    CASE WHEN subscribed THEN nullif(left(coalesce(p_error_message, ''), 1000), '')
      ELSE 'collection disabled/unsubscribed' END
  )
  ON CONFLICT (instrument_id, timeframe_minutes) DO UPDATE SET
    status = EXCLUDED.status,
    last_event_at = EXCLUDED.last_event_at,
    last_completed_open_time = EXCLUDED.last_completed_open_time,
    lag_ms = EXCLUDED.lag_ms,
    queue_depth = EXCLUDED.queue_depth,
    reconnect_count = EXCLUDED.reconnect_count,
    error_message = EXCLUDED.error_message,
    updated_at = clock_timestamp();
END;
$$;

CREATE FUNCTION public.claim_collector_lease(p_instance_id UUID, p_lease_seconds INTEGER DEFAULT 60)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_instance_id IS NULL OR p_lease_seconds < 30 OR p_lease_seconds > 300 THEN
    RAISE EXCEPTION 'Invalid collector lease';
  END IF;
  INSERT INTO public.collector_leases(lease_name, instance_id, leased_until)
    VALUES ('binance-usdm-public-market', p_instance_id,
      clock_timestamp() + make_interval(secs => p_lease_seconds))
  ON CONFLICT (lease_name) DO UPDATE SET
    instance_id = EXCLUDED.instance_id,
    leased_until = EXCLUDED.leased_until,
    updated_at = clock_timestamp()
  WHERE public.collector_leases.leased_until < clock_timestamp()
     OR public.collector_leases.instance_id = EXCLUDED.instance_id;
  RETURN FOUND;
END;
$$;

CREATE FUNCTION public.renew_collector_lease(p_instance_id UUID, p_lease_seconds INTEGER DEFAULT 60)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_instance_id IS NULL OR p_lease_seconds < 30 OR p_lease_seconds > 300 THEN
    RAISE EXCEPTION 'Invalid collector lease';
  END IF;
  UPDATE public.collector_leases SET
    leased_until = clock_timestamp() + make_interval(secs => p_lease_seconds),
    updated_at = clock_timestamp()
  WHERE lease_name = 'binance-usdm-public-market'
    AND instance_id = p_instance_id AND leased_until >= clock_timestamp();
  RETURN FOUND;
END;
$$;

CREATE FUNCTION public.release_collector_lease(p_instance_id UUID)
RETURNS VOID
LANGUAGE sql SECURITY INVOKER SET search_path = public
AS $$
  DELETE FROM public.collector_leases
  WHERE lease_name = 'binance-usdm-public-market' AND instance_id = p_instance_id;
$$;

CREATE FUNCTION public.get_collector_storage_diagnostics()
RETURNS TABLE (
  candle_rows BIGINT, health_rows BIGINT,
  oldest_candle_at TIMESTAMPTZ, newest_candle_at TIMESTAMPTZ
)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  SELECT
    (SELECT count(*) FROM public.collector_recent_candles),
    (SELECT count(*) FROM public.collector_health),
    (SELECT min(open_time) FROM public.collector_recent_candles),
    (SELECT max(open_time) FROM public.collector_recent_candles);
$$;

-- Read native completed-candle evidence for application-owned consumers. REST rows
-- have no exchange event timestamp; stored provenance is returned as recorded.
CREATE FUNCTION public.get_collector_completed_candles(
  p_symbol TEXT, p_timeframe_minutes INTEGER, p_limit INTEGER
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  normalized TEXT;
  recent JSONB;
BEGIN
  IF p_symbol IS NULL OR btrim(p_symbol) = '' THEN
    RAISE EXCEPTION 'Invalid collector completed candle request';
  END IF;
  IF p_timeframe_minutes IS NULL OR p_timeframe_minutes NOT IN (1, 15, 60, 240) THEN
    RAISE EXCEPTION 'Invalid collector completed candle timeframe';
  END IF;
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
    RAISE EXCEPTION 'Invalid collector completed candle limit';
  END IF;
  normalized := upper(btrim(p_symbol));
  SELECT coalesce(
      jsonb_agg(
        jsonb_build_object(
          'provider', newest.provider,
          'instrument_id', newest.instrument_id,
          'native_symbol', newest.native_symbol,
          'symbol', newest.symbol,
          'market_type', newest.market_type,
          'contract_type', newest.contract_type,
          'timeframe_minutes', newest.timeframe_minutes,
          'price_type', newest.price_type,
          'endpoint', newest.endpoint,
          'transport', newest.transport,
          'open_time_ms', (extract(epoch FROM newest.open_time) * 1000)::BIGINT,
          'close_time_ms', (extract(epoch FROM newest.close_time) * 1000)::BIGINT,
          'source_event_at_ms',
            CASE WHEN newest.source_event_at IS NULL THEN NULL
                 ELSE (extract(epoch FROM newest.source_event_at) * 1000)::BIGINT END,
          'received_at_ms', (extract(epoch FROM newest.received_at) * 1000)::BIGINT,
          'open', newest.open,
          'high', newest.high,
          'low', newest.low,
          'close', newest.close,
          'volume', newest.volume,
          'quote_volume', newest.quote_volume
        )
        ORDER BY newest.open_time
      ),
      '[]'::jsonb
    )
    INTO recent
    FROM (
      SELECT c.provider, c.instrument_id, c.symbol, c.native_symbol, c.market_type,
             c.contract_type, c.timeframe_minutes, c.price_type, c.endpoint,
             c.transport, c.open_time, c.close_time, c.source_event_at, c.received_at,
             c.open, c.high, c.low, c.close, c.volume, c.quote_volume
      FROM public.collector_recent_candles c
      WHERE c.provider = 'binance-usdm'
        AND c.price_type = 'trade'
        AND c.symbol = normalized
        AND c.timeframe_minutes = p_timeframe_minutes
      ORDER BY c.open_time DESC
      LIMIT p_limit
    ) newest;
  RETURN recent;
END;
$$;

CREATE FUNCTION public.upsert_market_state_current(
  p_universe_id TEXT,
  p_primary_window_minutes INTEGER,
  p_universe_version TEXT,
  p_provider TEXT,
  p_exchange TEXT,
  p_price_type TEXT,
  p_evaluation_boundary_time TIMESTAMPTZ,
  p_direction_state TEXT,
  p_pace TEXT,
  p_active_episode_id TEXT,
  p_active_direction TEXT,
  p_interrupted BOOLEAN,
  p_episode_algorithm_version TEXT,
  p_lifecycle_config_version TEXT,
  p_classifier_algorithm_version TEXT,
  p_classifier_config_version TEXT,
  p_movement_algorithm_version TEXT,
  p_movement_config_version TEXT,
  p_lifecycle_state JSONB,
  p_current_evidence JSONB
) RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  INSERT INTO public.market_state_current (
    universe_id, primary_window_minutes, universe_version, provider, exchange,
    price_type, evaluation_boundary_time, direction_state, pace,
    active_episode_id, active_direction, interrupted,
    episode_algorithm_version, lifecycle_config_version,
    classifier_algorithm_version, classifier_config_version,
    movement_algorithm_version, movement_config_version,
    lifecycle_state, current_evidence, updated_at
  ) VALUES (
    p_universe_id, p_primary_window_minutes, p_universe_version, p_provider, p_exchange,
    p_price_type, p_evaluation_boundary_time, p_direction_state, p_pace,
    p_active_episode_id, p_active_direction, p_interrupted,
    p_episode_algorithm_version, p_lifecycle_config_version,
    p_classifier_algorithm_version, p_classifier_config_version,
    p_movement_algorithm_version, p_movement_config_version,
    p_lifecycle_state, coalesce(p_current_evidence, '{}'::jsonb),
    clock_timestamp()
  )
  ON CONFLICT (universe_id, primary_window_minutes) DO UPDATE SET
    universe_version = EXCLUDED.universe_version,
    provider = EXCLUDED.provider,
    exchange = EXCLUDED.exchange,
    price_type = EXCLUDED.price_type,
    evaluation_boundary_time = EXCLUDED.evaluation_boundary_time,
    direction_state = EXCLUDED.direction_state,
    pace = EXCLUDED.pace,
    active_episode_id = EXCLUDED.active_episode_id,
    active_direction = EXCLUDED.active_direction,
    interrupted = EXCLUDED.interrupted,
    episode_algorithm_version = EXCLUDED.episode_algorithm_version,
    lifecycle_config_version = EXCLUDED.lifecycle_config_version,
    classifier_algorithm_version = EXCLUDED.classifier_algorithm_version,
    classifier_config_version = EXCLUDED.classifier_config_version,
    movement_algorithm_version = EXCLUDED.movement_algorithm_version,
    movement_config_version = EXCLUDED.movement_config_version,
    lifecycle_state = EXCLUDED.lifecycle_state,
    current_evidence = EXCLUDED.current_evidence,
    updated_at = clock_timestamp()
  WHERE public.market_state_current.evaluation_boundary_time <= EXCLUDED.evaluation_boundary_time;
END;
$$;

CREATE FUNCTION public.append_market_movement_event(p_canonical_event JSONB) RETURNS TEXT
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  v_inserted TEXT;
BEGIN
  INSERT INTO public.market_movement_events (
    event_id, episode_id, previous_episode_id, transition, transition_reason,
    episode_algorithm_version, lifecycle_config_version,
    from_direction, to_direction, episode_start_boundary_time, evaluation_boundary_time,
    universe_id, universe_version, primary_window_minutes, provider, exchange,
    price_type, direction, pace, directional_breadth, material_breadth,
    median_raw_return, median_normalized_movement, median_acceleration,
    acceleration_breadth, dispersion, rvol_summary, outliers,
    supporting_contracts, conflicting_contracts, configured_universe,
    included_symbols, excluded_symbols, windows_context,
    classifier_algorithm_version, classifier_config_version,
    movement_algorithm_version, movement_config_version, canonical_event, created_at
  ) VALUES (
    p_canonical_event->>'event_id', p_canonical_event->>'episode_id',
    p_canonical_event->>'previous_episode_id', p_canonical_event->>'transition',
    p_canonical_event->>'transition_reason',
    p_canonical_event->'episode_scope'->>'lifecycle_algorithm_version',
    p_canonical_event->'episode_scope'->>'lifecycle_config_version',
    p_canonical_event->>'from_direction', p_canonical_event->>'to_direction',
    to_timestamp((p_canonical_event->>'episode_start_boundary_time_ms')::double precision / 1000.0),
    to_timestamp((p_canonical_event->>'evaluation_boundary_time_ms')::double precision / 1000.0),
    p_canonical_event->'episode_scope'->>'universe_id',
    p_canonical_event->'episode_scope'->>'universe_version',
    (p_canonical_event->'episode_scope'->>'primary_window_minutes')::integer,
    p_canonical_event->'episode_scope'->>'provider',
    p_canonical_event->'episode_scope'->>'exchange',
    p_canonical_event->'episode_scope'->>'price_type',
    p_canonical_event->>'episode_direction',
    CASE WHEN p_canonical_event->'pace'->>'available' = 'true'
      THEN p_canonical_event->'pace'->>'value' ELSE 'NOT_APPLICABLE' END,
    CASE WHEN p_canonical_event->'directional_breadth'->>'available' = 'true'
      THEN (p_canonical_event->'directional_breadth'->'value'->>'fraction')::double precision END,
    CASE WHEN p_canonical_event->'material_breadth'->>'available' = 'true'
      THEN (p_canonical_event->'material_breadth'->'value'->>'fraction')::double precision END,
    CASE WHEN p_canonical_event->'median_raw_return'->>'available' = 'true'
      THEN (p_canonical_event->'median_raw_return'->>'value')::double precision END,
    CASE WHEN p_canonical_event->'median_normalized_movement'->>'available' = 'true'
      THEN (p_canonical_event->'median_normalized_movement'->>'value')::double precision END,
    CASE WHEN p_canonical_event->'median_acceleration'->>'available' = 'true'
      THEN (p_canonical_event->'median_acceleration'->>'value')::double precision END,
    CASE WHEN p_canonical_event->'acceleration_breadth'->>'available' = 'true'
      THEN (p_canonical_event->'acceleration_breadth'->'value'->>'fraction')::double precision END,
    CASE WHEN p_canonical_event->'dispersion_mad_normalized_movement'->>'available' = 'true'
      THEN (p_canonical_event->'dispersion_mad_normalized_movement'->>'value')::double precision END,
    coalesce(p_canonical_event->'volume_context', '[]'::jsonb),
    coalesce(p_canonical_event->'isolated_outliers', '[]'::jsonb),
    coalesce(p_canonical_event->'supporting_contracts', '[]'::jsonb),
    coalesce(p_canonical_event->'conflicting_contracts', '[]'::jsonb),
    coalesce(p_canonical_event->'configured_universe', '[]'::jsonb),
    coalesce(p_canonical_event->'included_symbols', '[]'::jsonb),
    coalesce(p_canonical_event->'excluded_symbols', '[]'::jsonb),
    coalesce(p_canonical_event->'windows_context', '[]'::jsonb),
    p_canonical_event->'episode_scope'->>'classifier_algorithm_version',
    p_canonical_event->'episode_scope'->>'classifier_config_version',
    p_canonical_event->'episode_scope'->>'movement_algorithm_version',
    p_canonical_event->'episode_scope'->>'movement_config_version',
    p_canonical_event, clock_timestamp()
  )
  ON CONFLICT (event_id) DO NOTHING
  RETURNING event_id INTO v_inserted;

  IF v_inserted IS NOT NULL THEN
    RETURN 'appended';
  ELSE
    RETURN 'already_exists';
  END IF;
END;
$$;

CREATE FUNCTION public.persist_market_episode_lifecycle_step(
  p_current_state JSONB,
  p_events JSONB
) RETURNS JSONB
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  v_event JSONB;
  v_status TEXT;
  v_statuses JSONB := '[]'::jsonb;
BEGIN
  FOR v_event IN
    SELECT value FROM jsonb_array_elements(coalesce(p_events, '[]'::jsonb))
  LOOP
    v_status := public.append_market_movement_event(v_event);
    v_statuses := v_statuses || jsonb_build_array(
      jsonb_build_object('eventId', v_event->>'event_id', 'status', v_status)
    );
  END LOOP;

  PERFORM public.upsert_market_state_current(
    p_current_state->>'universeId',
    (p_current_state->>'primaryWindowMinutes')::integer,
    p_current_state->>'universeVersion',
    p_current_state->>'provider',
    p_current_state->>'exchange',
    p_current_state->>'priceType',
    to_timestamp(((p_current_state->>'evaluationBoundaryTime')::double precision) / 1000.0),
    p_current_state->>'directionState',
    p_current_state->>'pace',
    p_current_state->>'activeEpisodeId',
    p_current_state->>'activeDirection',
    (p_current_state->>'interrupted')::boolean,
    p_current_state->>'episodeAlgorithmVersion',
    p_current_state->>'lifecycleConfigVersion',
    p_current_state->>'classifierAlgorithmVersion',
    p_current_state->>'classifierConfigVersion',
    p_current_state->>'movementAlgorithmVersion',
    p_current_state->>'movementConfigVersion',
    p_current_state->'lifecycleState',
    p_current_state->'currentEvidence'
  );

  RETURN v_statuses;
END;
$$;

-- Caller-supplied #71 normalization history: canonical completed one-minute
-- candles for causal window returns and participation, without fabricated 5s data.
CREATE FUNCTION public.get_collector_movement_candles(
  p_symbols TEXT[], p_since TIMESTAMPTZ, p_before_boundary TIMESTAMPTZ
) RETURNS JSONB
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  SELECT coalesce(jsonb_object_agg(symbol, rows), '{}'::jsonb)
  FROM (
    SELECT symbol,
      jsonb_agg(
        jsonb_build_array(
          (extract(epoch FROM open_time) * 1000)::BIGINT,
          close,
          volume,
          quote_volume
        )
        ORDER BY open_time
      ) AS rows
    FROM public.collector_recent_candles
    WHERE timeframe_minutes = 1
      AND symbol = ANY(p_symbols)
      AND open_time >= p_since
      AND open_time + interval '1 minute' < p_before_boundary
    GROUP BY symbol
  ) grouped;
$$;

-- Forward engine input (#239): completed one-minute candles of one symbol in
-- [p_since, p_before) as compact arrays with their provenance, at most 63 days.
CREATE FUNCTION public.get_collector_forward_minutes(
  p_symbol TEXT, p_since TIMESTAMPTZ, p_before TIMESTAMPTZ
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  rows JSONB;
BEGIN
  IF p_symbol IS NULL OR btrim(p_symbol) = '' OR p_since IS NULL OR p_before IS NULL
     OR p_before <= p_since OR p_before - p_since > interval '63 days' THEN
    RAISE EXCEPTION 'Invalid forward minute candle request';
  END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_array(
      (extract(epoch FROM open_time) * 1000)::BIGINT, open, high, low, close, volume, transport,
      CASE WHEN source_event_at IS NULL THEN NULL
           ELSE (extract(epoch FROM source_event_at) * 1000)::BIGINT END
    ) ORDER BY open_time), '[]'::jsonb)
    INTO rows
    FROM public.collector_recent_candles
    WHERE provider = 'binance-usdm' AND price_type = 'trade' AND timeframe_minutes = 1
      AND symbol = upper(btrim(p_symbol)) AND open_time >= p_since AND open_time < p_before;
  RETURN rows;
END;
$$;

-- Function access matches the current operational API. The episode RPCs retain
-- default EXECUTE privileges; their underlying tables remain service-role-only.
REVOKE ALL ON FUNCTION
  public.record_recent_candles(UUID, JSONB, INTEGER),
  public.record_market_data_checkpoint(UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, TIMESTAMPTZ, DOUBLE PRECISION),
  public.record_monitor_run(UUID, TEXT, INTEGER, INTEGER, TEXT, TEXT, INTEGER, JSONB, INTEGER),
  public.stage_durable_result(UUID, UUID, UUID, TEXT, JSONB),
  public.claim_sync_outbox(UUID, INTEGER),
  public.mark_sync_outbox_delivered(UUID, UUID),
  public.mark_sync_outbox_failed(UUID, UUID, TEXT, INTEGER),
  public.get_storage_diagnostics(UUID),
  public.get_global_storage_diagnostics(),
  public.purge_delivered_results(INTEGER),
  public.assign_collector_subscriptions(TEXT[]),
  public.get_collector_subscriptions(),
  public.record_collector_health(TEXT, TEXT, INTEGER, TEXT, TIMESTAMPTZ, TIMESTAMPTZ, BIGINT, INTEGER, INTEGER, TEXT),
  public.claim_collector_lease(UUID, INTEGER),
  public.renew_collector_lease(UUID, INTEGER),
  public.release_collector_lease(UUID),
  public.get_collector_storage_diagnostics(),
  public.get_collector_completed_candles(TEXT, INTEGER, INTEGER),
  public.get_collector_movement_candles(TEXT[], TIMESTAMPTZ, TIMESTAMPTZ),
  public.get_collector_forward_minutes(TEXT, TIMESTAMPTZ, TIMESTAMPTZ),
  public.record_collector_candles(JSONB, INTEGER, INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION
  public.record_recent_candles(UUID, JSONB, INTEGER),
  public.record_market_data_checkpoint(UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, TIMESTAMPTZ, DOUBLE PRECISION),
  public.record_monitor_run(UUID, TEXT, INTEGER, INTEGER, TEXT, TEXT, INTEGER, JSONB, INTEGER),
  public.stage_durable_result(UUID, UUID, UUID, TEXT, JSONB),
  public.claim_sync_outbox(UUID, INTEGER),
  public.mark_sync_outbox_delivered(UUID, UUID),
  public.mark_sync_outbox_failed(UUID, UUID, TEXT, INTEGER),
  public.get_storage_diagnostics(UUID),
  public.get_global_storage_diagnostics(),
  public.purge_delivered_results(INTEGER),
  public.assign_collector_subscriptions(TEXT[]),
  public.get_collector_subscriptions(),
  public.record_collector_health(TEXT, TEXT, INTEGER, TEXT, TIMESTAMPTZ, TIMESTAMPTZ, BIGINT, INTEGER, INTEGER, TEXT),
  public.claim_collector_lease(UUID, INTEGER),
  public.renew_collector_lease(UUID, INTEGER),
  public.release_collector_lease(UUID),
  public.get_collector_storage_diagnostics(),
  public.get_collector_completed_candles(TEXT, INTEGER, INTEGER),
  public.get_collector_movement_candles(TEXT[], TIMESTAMPTZ, TIMESTAMPTZ),
  public.get_collector_forward_minutes(TEXT, TIMESTAMPTZ, TIMESTAMPTZ),
  public.record_collector_candles(JSONB, INTEGER, INTEGER)
  TO service_role;

-- Forward trend daily feed (#239 P14): completed UTC-day Binance USD-M klines for the daily trend
-- track. Append-only (a stored day is never changed or deleted), one row per symbol and day, and only
-- completed days: a row must be received after its day ended. Gaps stay missing, never filled.
CREATE TABLE public.forward_daily_bars (
  symbol TEXT NOT NULL CHECK (symbol ~ '^[A-Z0-9]{5,16}$'),
  day DATE NOT NULL,
  open NUMERIC NOT NULL CHECK (open > 0),
  high NUMERIC NOT NULL CHECK (high > 0),
  low NUMERIC NOT NULL CHECK (low > 0),
  close NUMERIC NOT NULL CHECK (close > 0),
  volume NUMERIC NOT NULL CHECK (volume >= 0),
  quote_volume NUMERIC NOT NULL CHECK (quote_volume >= 0),
  source TEXT NOT NULL CHECK (source = 'binance-usdm:/fapi/v1/klines?interval=1d'),
  received_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (symbol, day),
  CHECK (low <= least(open, close) AND greatest(open, close) <= high),
  CHECK (received_at >= (day + 1)::timestamp AT TIME ZONE 'UTC')
);

CREATE FUNCTION public.reject_forward_daily_bar_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
  RAISE EXCEPTION 'forward daily bars are append-only'
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER forward_daily_bars_append_only BEFORE UPDATE OR DELETE ON public.forward_daily_bars
  FOR EACH ROW EXECUTE FUNCTION public.reject_forward_daily_bar_mutation();
ALTER TABLE public.forward_daily_bars ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.forward_daily_bars FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON public.forward_daily_bars TO service_role;

-- Inserts completed daily bars; an existing (symbol, day) is left untouched. Returns rows inserted.
CREATE FUNCTION public.record_forward_daily_bars(p_rows JSONB)
RETURNS INTEGER
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  inserted INTEGER;
  conflict RECORD;
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' OR jsonb_array_length(p_rows) > 2000 THEN
    RAISE EXCEPTION 'Invalid forward daily bar batch';
  END IF;
  -- A day already stored must arrive with the same payload: a differing duplicate is never silently
  -- dropped (the caller would compute from values that are not the committed ones).
  SELECT r.symbol, r.day INTO conflict
    FROM jsonb_to_recordset(p_rows) AS r(symbol TEXT, day DATE, open NUMERIC, high NUMERIC, low NUMERIC,
      close NUMERIC, volume NUMERIC, quote_volume NUMERIC)
    JOIN public.forward_daily_bars b ON b.symbol = r.symbol AND b.day = r.day
    WHERE (b.open, b.high, b.low, b.close, b.volume, b.quote_volume)
      IS DISTINCT FROM (r.open, r.high, r.low, r.close, r.volume, r.quote_volume)
    ORDER BY r.day LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION 'forward daily bar conflict: % %', conflict.symbol, conflict.day
      USING ERRCODE = '23505';
  END IF;
  INSERT INTO public.forward_daily_bars
    (symbol, day, open, high, low, close, volume, quote_volume, source, received_at)
  SELECT r.symbol, r.day, r.open, r.high, r.low, r.close, r.volume, r.quote_volume, r.source, r.received_at
    FROM jsonb_to_recordset(p_rows) AS r(symbol TEXT, day DATE, open NUMERIC, high NUMERIC, low NUMERIC,
      close NUMERIC, volume NUMERIC, quote_volume NUMERIC, source TEXT, received_at TIMESTAMPTZ)
  ON CONFLICT (symbol, day) DO NOTHING;
  GET DIAGNOSTICS inserted = ROW_COUNT;
  RETURN inserted;
END;
$$;

-- Stored daily bars of one symbol from p_since (inclusive), oldest first, as compact arrays with the
-- prices as exact decimal text: [day_ms, open, high, low, close, volume, quote_volume].
CREATE FUNCTION public.get_forward_daily_bars(p_symbol TEXT, p_since DATE)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  rows JSONB;
BEGIN
  IF p_symbol IS NULL OR btrim(p_symbol) = '' OR p_since IS NULL THEN
    RAISE EXCEPTION 'Invalid forward daily bar request';
  END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_array(
      (extract(epoch FROM day::timestamp AT TIME ZONE 'UTC') * 1000)::BIGINT,
      open::TEXT, high::TEXT, low::TEXT, close::TEXT, volume::TEXT, quote_volume::TEXT
    ) ORDER BY day), '[]'::jsonb)
    INTO rows
    FROM public.forward_daily_bars
    WHERE symbol = upper(btrim(p_symbol)) AND day >= p_since;
  RETURN rows;
END;
$$;

REVOKE ALL ON FUNCTION
  public.record_forward_daily_bars(JSONB),
  public.get_forward_daily_bars(TEXT, DATE)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION
  public.record_forward_daily_bars(JSONB),
  public.get_forward_daily_bars(TEXT, DATE)
  TO service_role;
