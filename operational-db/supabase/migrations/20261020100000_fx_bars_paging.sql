-- Forex bar paging (#440). get_fx_bars returned at most the newest 20000 bars, so the hourly stage could
-- never read enough warm-up for the 240-minute strategies. A fifth parameter p_to_ms is an EXCLUSIVE upper
-- bound on open_time, so a caller can page backwards: read the newest page, then ask again with
-- p_to_ms = the oldest open_time_ms it got. The body, the 1..20000 limit check, the row shape and the
-- invoker rights are those of 20261019120000_fx_bars.sql (never edited; it is already applied).
-- The signature changes, so the old function is dropped (its grants go with it) and the new one is granted.
DROP FUNCTION public.get_fx_bars(TEXT, TEXT, BIGINT, INTEGER);

CREATE FUNCTION public.get_fx_bars(
  p_venue TEXT, p_instrument_id TEXT, p_from_ms BIGINT DEFAULT NULL, p_limit INTEGER DEFAULT 1000,
  p_to_ms BIGINT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  rows JSONB;
BEGIN
  IF p_venue IS NULL OR p_instrument_id IS NULL OR p_limit IS NULL OR p_limit < 1
     OR p_limit > 20000 THEN
    RAISE EXCEPTION 'Invalid fx bar request';
  END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_object(
      'time_basis', 'utc',
      'venue', newest.venue,
      'instrument_id', newest.instrument_id,
      'symbol', newest.symbol,
      'native_symbol', newest.native_symbol,
      'transport', newest.transport,
      'open_time_ms', (extract(epoch FROM newest.open_time) * 1000)::BIGINT,
      'open', newest.open,
      'high', newest.high,
      'low', newest.low,
      'close', newest.close,
      'spread_points', newest.spread_points,
      'tick_volume', newest.tick_volume,
      'received_at_ms', (extract(epoch FROM newest.received_at) * 1000)::BIGINT
    ) ORDER BY newest.open_time ASC), '[]'::jsonb)
    INTO rows
  FROM (
    SELECT b.*
    FROM public.fx_recent_bars b
    WHERE b.venue = p_venue AND b.instrument_id = p_instrument_id
      AND (p_from_ms IS NULL OR b.open_time >= timestamptz 'epoch' + p_from_ms * interval '1 millisecond')
      AND (p_to_ms IS NULL OR b.open_time < timestamptz 'epoch' + p_to_ms * interval '1 millisecond')
    ORDER BY b.open_time DESC
    LIMIT p_limit
  ) newest;
  RETURN rows;
END;
$$;

REVOKE ALL ON FUNCTION public.get_fx_bars(TEXT, TEXT, BIGINT, INTEGER, BIGINT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_fx_bars(TEXT, TEXT, BIGINT, INTEGER, BIGINT) TO service_role;
