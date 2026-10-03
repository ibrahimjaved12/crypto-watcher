-- Derived collector input. The application/orchestration side owns user
-- watchlists; it assigns the shared, supported symbol set here and the collector
-- worker reads it. This is collector input/state, never a second watchlist
-- authority, and the worker holds no Lovable credentials.

CREATE TABLE public.collector_subscriptions (
  universe_name TEXT PRIMARY KEY CHECK (universe_name = 'binance-usdm-shared'),
  symbols TEXT[] NOT NULL,
  assigned_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE public.collector_subscriptions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.collector_subscriptions FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.collector_subscriptions TO service_role;

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

CREATE OR REPLACE FUNCTION public.record_collector_health(
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

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;
