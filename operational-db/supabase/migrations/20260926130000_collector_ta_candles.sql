-- Read adapter for the application-owned completed-candle TA path (#20).
-- The leased collector is the canonical writer of completed Binance USD-M trade
-- klines; TanStack reads that same canonical history here instead of fetching a
-- second live exchange series while collector mode is active. Read-only, full
-- OHLCV, service-role only, external operational database.

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
        jsonb_build_array(
          (extract(epoch FROM newest.open_time) * 1000)::BIGINT,
          newest.open, newest.high, newest.low, newest.close, newest.volume
        )
        ORDER BY newest.open_time
      ),
      '[]'::jsonb
    )
    INTO recent
    FROM (
      SELECT c.open_time, c.open, c.high, c.low, c.close, c.volume
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
