-- Shared public Binance USD-M working state. External operational database only.
-- The lease makes one backend collector authoritative across a multi-instance fleet.

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
  CHECK (instrument_id = provider || ':' || native_symbol),
  CHECK (high >= greatest(open, close) AND low <= least(open, close) AND high >= low),
  CHECK (close_time = open_time + make_interval(mins => timeframe_minutes) - interval '1 millisecond'),
  CHECK (mod(extract(epoch FROM open_time)::BIGINT, timeframe_minutes * 60) = 0)
);
CREATE INDEX collector_recent_candles_time
  ON public.collector_recent_candles(open_time DESC);

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
-- watchlists; it assigns the shared, supported symbol set here and the collector
-- worker reads it. This is collector input/state, never a second watchlist
-- authority, and the worker holds no Lovable credentials.
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

ALTER TABLE public.collector_recent_candles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collector_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collector_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collector_leases ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.collector_recent_candles, public.collector_health,
  public.collector_subscriptions, public.collector_leases
  FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.collector_recent_candles, public.collector_health,
  public.collector_subscriptions, public.collector_leases
  TO service_role;

CREATE FUNCTION public.record_collector_candles(
  p_rows JSONB, p_retention_days INTEGER DEFAULT 7
) RETURNS TABLE (candle_identity TEXT)
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF jsonb_typeof(p_rows) <> 'array' OR jsonb_array_length(p_rows) < 1
     OR jsonb_array_length(p_rows) > 1000
     OR p_retention_days < 1 OR p_retention_days > 30 THEN
    RAISE EXCEPTION 'Invalid collector candle batch';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM jsonb_to_recordset(p_rows) AS x(
      provider TEXT, instrument_id TEXT, price_type TEXT, timeframe_minutes INTEGER,
      open_time TIMESTAMPTZ, close_time TIMESTAMPTZ, open DOUBLE PRECISION,
      high DOUBLE PRECISION, low DOUBLE PRECISION, close DOUBLE PRECISION,
      volume DOUBLE PRECISION, quote_volume DOUBLE PRECISION,
      source_event_at TIMESTAMPTZ
    )
    JOIN public.collector_recent_candles c USING (
      provider, instrument_id, price_type, timeframe_minutes, open_time
    )
    WHERE c.close_time <> x.close_time OR c.open <> x.open OR c.high <> x.high
      OR c.low <> x.low OR c.close <> x.close OR c.volume <> x.volume
      OR c.quote_volume IS DISTINCT FROM x.quote_volume
  ) THEN
    RAISE EXCEPTION 'Conflicting collector candle for stable identity';
  END IF;

  RETURN QUERY
  WITH inserted AS (
    INSERT INTO public.collector_recent_candles (
      instrument_id, symbol, native_symbol, provider, endpoint, price_type,
      timeframe_minutes, open_time, close_time, open, high, low, close, volume,
      quote_volume, source_event_at, received_at, transport
    )
    SELECT x.instrument_id, x.symbol, x.native_symbol, x.provider, x.endpoint,
      x.price_type, x.timeframe_minutes, x.open_time, x.close_time, x.open, x.high,
      x.low, x.close, x.volume, x.quote_volume, x.source_event_at, x.received_at,
      x.transport
    FROM jsonb_to_recordset(p_rows) AS x(
      instrument_id TEXT, symbol TEXT, native_symbol TEXT, provider TEXT, endpoint TEXT,
      price_type TEXT, timeframe_minutes INTEGER, open_time TIMESTAMPTZ,
      close_time TIMESTAMPTZ, open DOUBLE PRECISION, high DOUBLE PRECISION,
      low DOUBLE PRECISION, close DOUBLE PRECISION, volume DOUBLE PRECISION,
      quote_volume DOUBLE PRECISION, source_event_at TIMESTAMPTZ,
      received_at TIMESTAMPTZ, transport TEXT
    )
    ON CONFLICT (provider, instrument_id, price_type, timeframe_minutes, open_time)
      DO NOTHING
    RETURNING provider, instrument_id, price_type, timeframe_minutes, open_time
  )
  SELECT provider || '|' || instrument_id || '|' || price_type || '|'
    || timeframe_minutes::TEXT || '|' || (extract(epoch FROM open_time) * 1000)::BIGINT::TEXT
  FROM inserted;

  DELETE FROM public.collector_recent_candles
    WHERE close_time < clock_timestamp() - make_interval(days => p_retention_days);
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

-- Read the collector's canonical completed-candle evidence for application-owned TA.
-- REST rows have no exchange event timestamp; every stored provenance field is
-- returned as recorded, without reconstruction.
CREATE FUNCTION public.get_collector_ta_candles(
  p_symbol TEXT, p_timeframe_minutes INTEGER, p_limit INTEGER
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  normalized TEXT;
  recent JSONB;
BEGIN
  IF p_symbol IS NULL OR btrim(p_symbol) = '' THEN
    RAISE EXCEPTION 'Invalid collector TA candle request';
  END IF;
  IF p_timeframe_minutes IS NULL OR p_timeframe_minutes NOT IN (1, 15, 60, 240) THEN
    RAISE EXCEPTION 'Invalid collector TA candle timeframe';
  END IF;
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
    RAISE EXCEPTION 'Invalid collector TA candle limit';
  END IF;
  normalized := upper(btrim(p_symbol));
  SELECT coalesce(
      jsonb_agg(
        jsonb_build_object(
          'provider', newest.provider,
          'instrument_id', newest.instrument_id,
          'native_symbol', newest.native_symbol,
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
          'volume', newest.volume
        )
        ORDER BY newest.open_time
      ),
      '[]'::jsonb
    )
    INTO recent
    FROM (
      SELECT c.provider, c.instrument_id, c.native_symbol, c.price_type, c.endpoint,
             c.transport, c.open_time, c.close_time, c.source_event_at, c.received_at,
             c.open, c.high, c.low, c.close, c.volume
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

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;
