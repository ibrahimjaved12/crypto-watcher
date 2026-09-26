-- Provenance-preserving read adapter for the application-owned completed-candle TA
-- path (#20/#24/#26). The leased collector is the canonical writer of completed
-- Binance USD-M trade klines; the application reads that same canonical history here
-- instead of fetching a second live exchange series while collector mode is active.
--
-- The adapter must transport the collector's recorded evidence, not a reconstruction:
-- exact provider/instrument identity, endpoint, transport, candle open/close time,
-- source event time, receive time, and full OHLCV. It deliberately exposes no single
-- market-level endpoint or retrieval time, because one series mixes WebSocket live
-- candles with REST bootstrap/recovery candles.
--
-- `source_event_at` is the actual exchange event time and is nullable: REST
-- bootstrap/recovery has no exchange event, so it is recorded as NULL rather than an
-- invented timestamp. Candle completion is the deterministic boundary
-- `open_time + timeframe_minutes`, never `source_event_at`.
--
-- Replaces the reduced six-column payload from
-- 20260926130000_collector_ta_candles.sql. Read-only, service-role only, external
-- operational database.

-- The exchange event time is provenance, so REST rows legitimately have none.
ALTER TABLE public.collector_recent_candles ALTER COLUMN source_event_at DROP NOT NULL;
-- Drop the constraints that assumed a source event always exists and always follows
-- the collector receive time; the raw receive time is preserved as observed.
ALTER TABLE public.collector_recent_candles
  DROP CONSTRAINT IF EXISTS collector_recent_candles_check3;
ALTER TABLE public.collector_recent_candles
  DROP CONSTRAINT IF EXISTS collector_recent_candles_check4;
ALTER TABLE public.collector_recent_candles
  ADD CONSTRAINT collector_recent_candles_source_event_check
  CHECK (source_event_at IS NULL OR source_event_at >= close_time);

CREATE OR REPLACE FUNCTION public.get_collector_ta_candles(
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

REVOKE ALL ON FUNCTION public.get_collector_ta_candles(TEXT, INTEGER, INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_collector_ta_candles(TEXT, INTEGER, INTEGER)
  TO service_role;
