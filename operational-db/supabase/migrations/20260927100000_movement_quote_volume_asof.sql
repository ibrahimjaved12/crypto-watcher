-- Exact Binance quote notional for canonical #71 RVOL history and safe cutoff reads.
-- Existing rows remain NULL: their exact quote volume cannot be reconstructed from OHLCV.
ALTER TABLE public.collector_recent_candles
  ADD COLUMN quote_volume DOUBLE PRECISION
  CHECK (quote_volume IS NULL OR (quote_volume >= 0 AND quote_volume < 'Infinity'::float8));

CREATE OR REPLACE FUNCTION public.record_collector_candles(
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
      volume DOUBLE PRECISION, quote_volume DOUBLE PRECISION, source_event_at TIMESTAMPTZ
    )
    JOIN public.collector_recent_candles c USING (
      provider, instrument_id, price_type, timeframe_minutes, open_time
    )
    WHERE c.close_time <> x.close_time OR c.open <> x.open OR c.high <> x.high
      OR c.low <> x.low OR c.close <> x.close OR c.volume <> x.volume
      OR (c.quote_volume IS NOT NULL AND c.quote_volume IS DISTINCT FROM x.quote_volume)
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

  -- A legacy NULL can be enriched only by the exchange's exact quote-volume field
  -- on an otherwise identical candle; no value is inferred from price * base volume.
  UPDATE public.collector_recent_candles c SET quote_volume = x.quote_volume
  FROM jsonb_to_recordset(p_rows) AS x(
    provider TEXT, instrument_id TEXT, price_type TEXT, timeframe_minutes INTEGER,
    open_time TIMESTAMPTZ, close_time TIMESTAMPTZ, open DOUBLE PRECISION,
    high DOUBLE PRECISION, low DOUBLE PRECISION, close DOUBLE PRECISION,
    volume DOUBLE PRECISION, quote_volume DOUBLE PRECISION
  )
  WHERE x.quote_volume IS NOT NULL AND c.quote_volume IS NULL
    AND c.provider = x.provider AND c.instrument_id = x.instrument_id
    AND c.price_type = x.price_type AND c.timeframe_minutes = x.timeframe_minutes
    AND c.open_time = x.open_time AND c.close_time = x.close_time
    AND c.open = x.open AND c.high = x.high AND c.low = x.low
    AND c.close = x.close AND c.volume = x.volume;

  DELETE FROM public.collector_recent_candles
    WHERE close_time < clock_timestamp() - make_interval(days => p_retention_days);
END;
$$;

DROP FUNCTION public.get_collector_movement_candles(TEXT[], TIMESTAMPTZ);
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

REVOKE ALL ON FUNCTION public.get_collector_movement_candles(TEXT[], TIMESTAMPTZ, TIMESTAMPTZ)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_collector_movement_candles(TEXT[], TIMESTAMPTZ, TIMESTAMPTZ)
  TO service_role;
