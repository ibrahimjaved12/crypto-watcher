-- Refs #68. Preserve original candles and conflict evidence, including provisional REST rows.
-- Later 1m REST copies are evidence even outside research-parity tolerances.
-- No historical data rewrite and no forward signal recomputation.
CREATE OR REPLACE FUNCTION public.reconcile_conflicts(
  older_than_hours INTEGER DEFAULT 24,
  p_price_tolerance DOUBLE PRECISION DEFAULT 0.002,
  p_volume_tolerance DOUBLE PRECISION DEFAULT 0.02
) RETURNS INTEGER
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  written INTEGER;
BEGIN
  IF older_than_hours IS NULL OR older_than_hours < 0 OR older_than_hours > 24 * 365
     OR p_price_tolerance IS NULL OR p_price_tolerance < 0 OR p_price_tolerance > 0.05
     OR p_volume_tolerance IS NULL OR p_volume_tolerance < 0 OR p_volume_tolerance > 0.5 THEN
    RAISE EXCEPTION 'Invalid conflict reconciliation request';
  END IF;
  WITH eligible AS (
    SELECT k.*,
      (k.stored->>'high')::float8 AS s_high, (k.offered->>'high')::float8 AS o_high,
      (k.stored->>'low')::float8 AS s_low, (k.offered->>'low')::float8 AS o_low,
      (k.stored->>'close')::float8 AS s_close, (k.offered->>'close')::float8 AS o_close,
      (k.stored->>'volume')::float8 AS s_volume, (k.offered->>'volume')::float8 AS o_volume,
      (k.stored->>'quote_volume')::float8 AS s_quote, (k.offered->>'quote_volume')::float8 AS o_quote
    FROM public.collector_candle_conflicts k
    WHERE k.detected_at < clock_timestamp() - make_interval(hours => older_than_hours)
      AND (
        (k.timeframe_minutes = 1 AND k.offered_transport = 'rest')
        OR (k.rest_side = 'offered'
          AND k.differing_fields <@ ARRAY['high', 'low', 'close', 'volume', 'quote_volume'])
      )
      AND NOT EXISTS (
        SELECT 1 FROM public.collector_candle_revisions r WHERE r.conflict_id = k.conflict_id
      )
  ),
  within AS (
    SELECT e.* FROM eligible e
    WHERE (e.timeframe_minutes = 1 AND e.offered_transport = 'rest')
      OR (abs(e.o_high - e.s_high) <= p_price_tolerance * e.s_high
      AND abs(e.o_low - e.s_low) <= p_price_tolerance * e.s_low
      AND abs(e.o_close - e.s_close) <= p_price_tolerance * e.s_close
      AND abs(e.o_volume - e.s_volume) <= p_volume_tolerance * greatest(e.o_volume, e.s_volume)
      AND (e.o_quote IS NULL OR e.s_quote IS NULL
        OR abs(e.o_quote - e.s_quote) <= p_volume_tolerance * greatest(e.o_quote, e.s_quote)))
  ),
  appended AS (
    INSERT INTO public.collector_candle_revisions (
      revision_id, conflict_id, provider, instrument_id, symbol, price_type, timeframe_minutes,
      open_time, fields, source, supersedes, tolerance
    )
    SELECT md5(w.stored_hash || '|' || w.offered_hash), w.conflict_id, w.provider,
      w.instrument_id, w.symbol, w.price_type, w.timeframe_minutes, w.open_time,
      jsonb_build_object(
        'open', w.offered->'open', 'high', w.offered->'high', 'low', w.offered->'low',
        'close', w.offered->'close', 'volume', w.offered->'volume',
        'quote_volume', w.offered->'quote_volume', 'close_time', w.offered->'close_time',
        'candle_hash', w.offered_hash
      ),
      'rest', w.stored_hash,
      CASE WHEN w.timeframe_minutes = 1 AND w.offered_transport = 'rest'
        THEN jsonb_build_object('policy', 'later-rest-evidence')
        ELSE jsonb_build_object('price_relative', p_price_tolerance, 'volume_relative', p_volume_tolerance)
      END
    FROM within w
    ON CONFLICT DO NOTHING
    RETURNING 1
  )
  SELECT count(*)::INTEGER INTO written FROM appended;
  RETURN written;
END;
$$;
