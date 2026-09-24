-- External operational database only. Never apply this migration to Lovable.
-- TanStack's service role is the sole writer; browsers and Python have no access.

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
CREATE INDEX recent_candles_user_time
  ON public.recent_candles(user_id, open_time DESC);

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
CREATE INDEX monitor_runs_user_time ON public.monitor_runs(user_id, ran_at DESC);

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
CREATE INDEX sync_outbox_due
  ON public.sync_outbox(status, available_at, created_at)
  WHERE status IN ('pending', 'failed', 'processing');
CREATE INDEX sync_outbox_user ON public.sync_outbox(user_id, created_at DESC);

ALTER TABLE public.recent_candles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.market_data_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monitor_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.operational_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_outbox ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, anon, authenticated;
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;

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

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;
