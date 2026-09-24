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
  source_event_at TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  transport TEXT NOT NULL CHECK (transport IN ('rest', 'websocket')),
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (provider, instrument_id, price_type, timeframe_minutes, open_time),
  CHECK (instrument_id = provider || ':' || native_symbol),
  CHECK (high >= greatest(open, close) AND low <= least(open, close) AND high >= low),
  CHECK (close_time = open_time + make_interval(mins => timeframe_minutes) - interval '1 millisecond'),
  CHECK (source_event_at >= close_time),
  CHECK (received_at >= source_event_at),
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

CREATE TABLE public.collector_leases (
  lease_name TEXT PRIMARY KEY CHECK (lease_name = 'binance-usdm-public-market'),
  instance_id UUID NOT NULL,
  leased_until TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE public.collector_recent_candles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collector_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collector_leases ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.collector_recent_candles, public.collector_health, public.collector_leases
  FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.collector_recent_candles, public.collector_health, public.collector_leases
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
      volume DOUBLE PRECISION, source_event_at TIMESTAMPTZ
    )
    JOIN public.collector_recent_candles c USING (
      provider, instrument_id, price_type, timeframe_minutes, open_time
    )
    WHERE c.close_time <> x.close_time OR c.open <> x.open OR c.high <> x.high
      OR c.low <> x.low OR c.close <> x.close OR c.volume <> x.volume
  ) THEN
    RAISE EXCEPTION 'Conflicting collector candle for stable identity';
  END IF;

  RETURN QUERY
  WITH inserted AS (
    INSERT INTO public.collector_recent_candles (
      instrument_id, symbol, native_symbol, provider, endpoint, price_type,
      timeframe_minutes, open_time, close_time, open, high, low, close, volume,
      source_event_at, received_at, transport
    )
    SELECT x.instrument_id, x.symbol, x.native_symbol, x.provider, x.endpoint,
      x.price_type, x.timeframe_minutes, x.open_time, x.close_time, x.open, x.high,
      x.low, x.close, x.volume, x.source_event_at, x.received_at, x.transport
    FROM jsonb_to_recordset(p_rows) AS x(
      instrument_id TEXT, symbol TEXT, native_symbol TEXT, provider TEXT, endpoint TEXT,
      price_type TEXT, timeframe_minutes INTEGER, open_time TIMESTAMPTZ,
      close_time TIMESTAMPTZ, open DOUBLE PRECISION, high DOUBLE PRECISION,
      low DOUBLE PRECISION, close DOUBLE PRECISION, volume DOUBLE PRECISION,
      source_event_at TIMESTAMPTZ, received_at TIMESTAMPTZ, transport TEXT
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

CREATE FUNCTION public.record_collector_health(
  p_instrument_id TEXT, p_symbol TEXT, p_timeframe_minutes INTEGER, p_status TEXT,
  p_last_event_at TIMESTAMPTZ, p_last_completed_open_time TIMESTAMPTZ,
  p_lag_ms BIGINT, p_queue_depth INTEGER, p_reconnect_count INTEGER,
  p_error_message TEXT
) RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  INSERT INTO public.collector_health (
    instrument_id, symbol, timeframe_minutes, status, last_event_at,
    last_completed_open_time, lag_ms, queue_depth, reconnect_count, error_message
  ) VALUES (
    p_instrument_id, p_symbol, p_timeframe_minutes, p_status, p_last_event_at,
    p_last_completed_open_time, p_lag_ms, p_queue_depth, p_reconnect_count,
    nullif(left(coalesce(p_error_message, ''), 1000), '')
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

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;
