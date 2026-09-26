-- Pre-release destructive cutover for the completed-candle provenance model (#20/#24/#26).
-- External operational database only.
--
-- THIS IS A DESTRUCTIVE PRE-RELEASE RESET, NOT A PRODUCTION-COMPATIBLE MIGRATION.
-- Existing collector working state is disposable development data. It is wiped here and the
-- corrected schema is defined from scratch; the collector then rebuilds the required history
-- through its REST bootstrap. No compatibility column and no legacy-version marker is added.
--
-- Truthful timestamp semantics (three distinct concepts, never conflated):
--   * completion boundary = open_time + timeframe_minutes
--   * source_event_at     = the actual Binance exchange event time (WebSocket `E`), or NULL for
--                           REST bootstrap/recovery, which has no exchange event
--   * received_at         = the actual collector receive/retrieval time
--
-- Obsolete old-semantics constraints that assumed a source event always exists and always
-- follows the receive time (`source_event_at >= close_time`, `received_at >= source_event_at`)
-- are removed by definition, so the cutover does not depend on generated constraint names.

-- Wipe disposable collector working state so the next collector start performs a clean REST
-- bootstrap (candles, health, and the lease that gates the single authoritative writer).
TRUNCATE TABLE public.collector_recent_candles;
TRUNCATE TABLE public.collector_health;
TRUNCATE TABLE public.collector_leases;

-- The exchange event time is provenance, so REST rows legitimately have none.
ALTER TABLE public.collector_recent_candles ALTER COLUMN source_event_at DROP NOT NULL;

DO $$
DECLARE obsolete_constraint TEXT;
BEGIN
  FOR obsolete_constraint IN
    SELECT conname
    FROM pg_constraint
    WHERE conrelid = 'public.collector_recent_candles'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) ~ '(source_event_at|received_at)'
  LOOP
    EXECUTE format(
      'ALTER TABLE public.collector_recent_candles DROP CONSTRAINT %I', obsolete_constraint
    );
  END LOOP;
END;
$$;

ALTER TABLE public.collector_recent_candles
  ADD CONSTRAINT collector_recent_candles_source_event_check
  CHECK (source_event_at IS NULL OR source_event_at >= close_time);

-- Provenance-preserving read adapter for the application-owned completed-candle TA path. The
-- leased collector is the canonical writer of completed Binance USD-M trade klines; the
-- application reads that same canonical history here instead of fetching a second live exchange
-- series while collector mode is active. It transports the collector's recorded evidence — exact
-- provider/instrument identity, endpoint, transport, candle open/close time, exchange event time
-- (NULL for REST), receive time, and full OHLCV — and never reconstructs any of it. Read-only,
-- service-role only.
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
