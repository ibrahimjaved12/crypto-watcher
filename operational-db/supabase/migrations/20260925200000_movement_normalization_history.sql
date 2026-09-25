-- Caller-supplied #71 normalization history for the market-movement engine (Issue #74).
-- Returns canonical completed one-minute candles so the engine can derive comparable
-- window returns and participation without fabricating five-second history.
-- Service-role only, external operational database.

CREATE FUNCTION public.get_collector_movement_candles(
  p_symbols TEXT[], p_since TIMESTAMPTZ
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
          volume
        )
        ORDER BY open_time
      ) AS rows
    FROM public.collector_recent_candles
    WHERE timeframe_minutes = 1
      AND symbol = ANY(p_symbols)
      AND open_time >= p_since
    GROUP BY symbol
  ) grouped;
$$;

REVOKE ALL ON FUNCTION public.get_collector_movement_candles(TEXT[], TIMESTAMPTZ)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_collector_movement_candles(TEXT[], TIMESTAMPTZ)
  TO service_role;
