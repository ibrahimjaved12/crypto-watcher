-- Forex collector gap checks (#457, part of #374). Operational database only.
--
-- The collector's resume rule (newest stored minute + 1) never revisits a hole behind the newest
-- stored minute. The gap scan looks for open-market minutes (TypeScript FX week calendar) that have
-- no stored bar and asks the gateway for them again. Every range the gateway answered 200 for is
-- appended here, with how many missing minutes were asked for and how many came back, so a minute
-- MT5 has no bar for is not asked for again until the recheck window has passed. A check with
-- returned_minutes < requested_minutes is "confirmed empty" evidence, never a data source.
-- The objects of 20261019120000_fx_bars.sql are not touched.

CREATE TABLE public.fx_gap_checks (
  check_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  venue TEXT NOT NULL CHECK (venue ~ '^[a-z0-9-]{3,40}$'),
  instrument_id TEXT NOT NULL CHECK (instrument_id ~ '^(fx|fut):[A-Z0-9._-]{2,20}$'),
  -- [from_ms, to_ms): the requested range, UTC minute boundaries.
  from_ms BIGINT NOT NULL CHECK (from_ms >= 0 AND mod(from_ms, 60000) = 0),
  to_ms BIGINT NOT NULL CHECK (mod(to_ms, 60000) = 0),
  requested_minutes INTEGER NOT NULL CHECK (requested_minutes >= 1),
  returned_minutes INTEGER NOT NULL CHECK (returned_minutes >= 0),
  checked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CHECK (from_ms < to_ms),
  CHECK (requested_minutes <= (to_ms - from_ms) / 60000),
  CHECK (returned_minutes <= requested_minutes)
);
CREATE INDEX fx_gap_checks_recent ON public.fx_gap_checks(venue, instrument_id, checked_at DESC);

CREATE TRIGGER fx_gap_checks_append_only
  BEFORE UPDATE OR DELETE ON public.fx_gap_checks
  FOR EACH ROW EXECUTE FUNCTION public.reject_candle_evidence_mutation();

ALTER TABLE public.fx_gap_checks ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.fx_gap_checks FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.fx_gap_checks TO service_role;

-- Items: {venue, instrument_id, from_ms, to_ms, requested_minutes, returned_minutes}, numbers as
-- JSON integers; 1 to 100 per call. Any invalid item rejects the whole call with one generic message.
-- CASE keeps the casts behind the type checks.
CREATE FUNCTION public.record_fx_gap_checks(p_rows JSONB)
RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' OR jsonb_array_length(p_rows) < 1
     OR jsonb_array_length(p_rows) > 100
     OR EXISTS (
       SELECT 1 FROM jsonb_array_elements(p_rows) AS item(value)
       WHERE CASE
         WHEN jsonb_typeof(item.value) <> 'object' THEN true
         WHEN jsonb_typeof(item.value->'venue') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item.value->'instrument_id') IS DISTINCT FROM 'string' THEN true
         WHEN item.value->>'venue' !~ '^[a-z0-9-]{3,40}$'
           OR item.value->>'instrument_id' !~ '^(fx|fut):[A-Z0-9._-]{2,20}$' THEN true
         WHEN jsonb_typeof(item.value->'from_ms') IS DISTINCT FROM 'number'
           OR jsonb_typeof(item.value->'to_ms') IS DISTINCT FROM 'number'
           OR jsonb_typeof(item.value->'requested_minutes') IS DISTINCT FROM 'number'
           OR jsonb_typeof(item.value->'returned_minutes') IS DISTINCT FROM 'number' THEN true
         WHEN (item.value->>'from_ms')::NUMERIC <> trunc((item.value->>'from_ms')::NUMERIC)
           OR (item.value->>'to_ms')::NUMERIC <> trunc((item.value->>'to_ms')::NUMERIC)
           OR (item.value->>'requested_minutes')::NUMERIC <> trunc((item.value->>'requested_minutes')::NUMERIC)
           OR (item.value->>'returned_minutes')::NUMERIC <> trunc((item.value->>'returned_minutes')::NUMERIC) THEN true
         WHEN (item.value->>'from_ms')::NUMERIC < 0
           OR (item.value->>'to_ms')::NUMERIC > 9007199254740991
           OR mod((item.value->>'from_ms')::NUMERIC, 60000) <> 0
           OR mod((item.value->>'to_ms')::NUMERIC, 60000) <> 0
           OR (item.value->>'from_ms')::NUMERIC >= (item.value->>'to_ms')::NUMERIC THEN true
         WHEN (item.value->>'requested_minutes')::NUMERIC < 1
           OR (item.value->>'requested_minutes')::NUMERIC
             > ((item.value->>'to_ms')::NUMERIC - (item.value->>'from_ms')::NUMERIC) / 60000
           OR (item.value->>'returned_minutes')::NUMERIC < 0
           OR (item.value->>'returned_minutes')::NUMERIC
             > (item.value->>'requested_minutes')::NUMERIC THEN true
         ELSE false
       END
     ) THEN
    RAISE EXCEPTION 'Invalid fx gap checks';
  END IF;

  INSERT INTO public.fx_gap_checks (
    venue, instrument_id, from_ms, to_ms, requested_minutes, returned_minutes
  )
  SELECT item.value->>'venue', item.value->>'instrument_id',
    (item.value->>'from_ms')::NUMERIC::BIGINT, (item.value->>'to_ms')::NUMERIC::BIGINT,
    (item.value->>'requested_minutes')::NUMERIC::INTEGER,
    (item.value->>'returned_minutes')::NUMERIC::INTEGER
  FROM jsonb_array_elements(p_rows) AS item(value);
END;
$$;

-- The checks of one series made at or after p_since_ms, newest first, at most 500.
CREATE FUNCTION public.get_fx_gap_checks(p_venue TEXT, p_instrument_id TEXT, p_since_ms BIGINT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  rows JSONB;
BEGIN
  IF p_venue IS NULL OR p_instrument_id IS NULL OR p_since_ms IS NULL THEN
    RAISE EXCEPTION 'Invalid fx gap check request';
  END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_object(
      'from_ms', recent.from_ms,
      'to_ms', recent.to_ms,
      'checked_at_ms', (extract(epoch FROM recent.checked_at) * 1000)::BIGINT,
      'requested_minutes', recent.requested_minutes,
      'returned_minutes', recent.returned_minutes
    ) ORDER BY recent.checked_at DESC, recent.check_id DESC), '[]'::jsonb)
    INTO rows
  FROM (
    SELECT c.*
    FROM public.fx_gap_checks c
    WHERE c.venue = p_venue AND c.instrument_id = p_instrument_id
      AND c.checked_at >= timestamptz 'epoch' + p_since_ms * interval '1 millisecond'
    ORDER BY c.checked_at DESC, c.check_id DESC
    LIMIT 500
  ) recent;
  RETURN rows;
END;
$$;

REVOKE ALL ON FUNCTION
  public.record_fx_gap_checks(JSONB),
  public.get_fx_gap_checks(TEXT, TEXT, BIGINT)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION
  public.record_fx_gap_checks(JSONB),
  public.get_fx_gap_checks(TEXT, TEXT, BIGINT)
  TO service_role;
