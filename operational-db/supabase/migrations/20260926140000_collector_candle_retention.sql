-- Retention must not make the application-owned completed-candle TA path (#20)
-- permanently unusable. The day-based window still bounds the bulk of the store, but
-- every canonical (provider, instrument, price type, timeframe) series always keeps
-- its newest completed candles so the TA minimum history (200) plus the bounded
-- catch-up batch is always available, whatever the frame length.
--
-- Replaces the retention step of record_collector_candles from
-- 20260925120000_binance_collector.sql. Service-role only, external operational DB.

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

  -- Keep the newest 260 completed candles per canonical series. That covers the TA
  -- minimum history (200) and the bounded catch-up batch with margin, so a longer
  -- frame (1h, 4h) keeps enough history even when it spans more than the day window.
  DELETE FROM public.collector_recent_candles c
    WHERE c.close_time < clock_timestamp() - make_interval(days => p_retention_days)
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

REVOKE ALL ON FUNCTION public.record_collector_candles(JSONB, INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.record_collector_candles(JSONB, INTEGER)
  TO service_role;
