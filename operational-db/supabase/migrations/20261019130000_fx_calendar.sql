-- Economic calendar store for the forex news blackout (#401 part 1, part of #374). Operational
-- database only; written by a future headless fetcher child with operational credentials (part 2).
--
-- python/market_analysis/news_blackout.py uses an event only when it was already known at the
-- decision time (first_seen_ms <= ts_ms). This store therefore records HOW the schedule looked
-- over time: every distinct (source, key, time, impact, title) is an append-only observation with
-- the time it was first seen. A rescheduled, re-rated or re-titled event is a NEW observation, never
-- an update. The fetch log lets readers fail closed when the feed has silently stopped. No crypto
-- object is touched.

CREATE TABLE public.fx_calendar_observations (
  observation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source TEXT NOT NULL CHECK (source ~ '^[a-z0-9-]{2,40}$'),
  source_event_key TEXT NOT NULL CHECK (length(source_event_key) BETWEEN 1 AND 200),
  event_ts TIMESTAMPTZ NOT NULL CHECK (isfinite(event_ts)),
  currency TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  impact TEXT NOT NULL CHECK (impact IN ('low', 'medium', 'high')),
  title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
  -- forecast/previous/actual as the source gives them; not used by the filter.
  detail JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(detail) = 'object'),
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (source, source_event_key, event_ts, impact, title)
);
CREATE INDEX fx_calendar_observations_time ON public.fx_calendar_observations(event_ts);

-- Kept forever (a few thousand rows a year) and never changed.
CREATE TRIGGER fx_calendar_observations_append_only
  BEFORE UPDATE OR DELETE ON public.fx_calendar_observations
  FOR EACH ROW EXECUTE FUNCTION public.reject_candle_evidence_mutation();

-- One row per fetch attempt. covered_to is the end of the range an ok response covers (e.g. the end
-- of the fetched week); an error covers nothing. Rows older than 90 days are deleted on write.
CREATE TABLE public.fx_calendar_fetches (
  fetch_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source TEXT NOT NULL CHECK (source ~ '^[a-z0-9-]{2,40}$'),
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
  event_count INTEGER NOT NULL CHECK (event_count >= 0),
  error_message TEXT CHECK (error_message IS NULL OR length(error_message) <= 1000),
  covered_to TIMESTAMPTZ CHECK (covered_to IS NULL OR isfinite(covered_to)),
  CHECK ((status = 'ok') = (covered_to IS NOT NULL))
);
CREATE INDEX fx_calendar_fetches_recent ON public.fx_calendar_fetches(source, fetched_at DESC);

ALTER TABLE public.fx_calendar_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fx_calendar_fetches ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.fx_calendar_observations, public.fx_calendar_fetches
  FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.fx_calendar_observations, public.fx_calendar_fetches TO service_role;

-- Items: {source, source_event_key, event_ts (ISO 8601 with an explicit offset), currency, impact,
-- title, detail?}; 1 to 2000 per call. A known observation is skipped. Returns the number of NEW
-- observations. Any invalid item rejects the whole call (nothing is partly written).
CREATE FUNCTION public.record_fx_calendar(p_rows JSONB)
RETURNS INTEGER
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  written INTEGER;
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' THEN
    RAISE EXCEPTION 'Invalid fx calendar batch';
  END IF;
  IF jsonb_array_length(p_rows) < 1 OR jsonb_array_length(p_rows) > 2000
     OR EXISTS (
       SELECT 1 FROM jsonb_array_elements(p_rows) AS item(value)
       WHERE jsonb_typeof(item.value) <> 'object'
         OR jsonb_typeof(item.value->'source') IS DISTINCT FROM 'string'
         OR jsonb_typeof(item.value->'source_event_key') IS DISTINCT FROM 'string'
         OR jsonb_typeof(item.value->'event_ts') IS DISTINCT FROM 'string'
         OR jsonb_typeof(item.value->'currency') IS DISTINCT FROM 'string'
         OR jsonb_typeof(item.value->'impact') IS DISTINCT FROM 'string'
         OR jsonb_typeof(item.value->'title') IS DISTINCT FROM 'string'
         OR coalesce(jsonb_typeof(item.value->'detail'), 'null') NOT IN ('object', 'null')
         OR item.value->>'source' !~ '^[a-z0-9-]{2,40}$'
         OR length(item.value->>'source_event_key') NOT BETWEEN 1 AND 200
         -- No offset would silently mean the session time zone.
         OR item.value->>'event_ts' !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$'
         OR item.value->>'currency' !~ '^[A-Z]{3}$'
         OR item.value->>'impact' NOT IN ('low', 'medium', 'high')
         OR length(item.value->>'title') NOT BETWEEN 1 AND 300
     ) THEN
    RAISE EXCEPTION 'Invalid fx calendar batch';
  END IF;

  BEGIN
    INSERT INTO public.fx_calendar_observations (
      source, source_event_key, event_ts, currency, impact, title, detail
    )
    SELECT item.value->>'source', item.value->>'source_event_key',
      (item.value->>'event_ts')::TIMESTAMPTZ, item.value->>'currency', item.value->>'impact',
      item.value->>'title',
      COALESCE(nullif(item.value->'detail', 'null'::jsonb), '{}'::jsonb)
    FROM jsonb_array_elements(p_rows) WITH ORDINALITY AS item(value, position)
    ORDER BY item.position
    ON CONFLICT (source, source_event_key, event_ts, impact, title) DO NOTHING;
    GET DIAGNOSTICS written = ROW_COUNT;
  EXCEPTION
    -- An unparsable timestamp (e.g. month 13) or a table CHECK: the block is rolled back.
    WHEN invalid_datetime_format OR datetime_field_overflow OR check_violation
      OR not_null_violation THEN
      RAISE EXCEPTION 'Invalid fx calendar batch: %', SQLERRM;
  END;
  RETURN written;
END;
$$;

-- Log one fetch attempt. An ok fetch must say how far its response covers; an error covers nothing.
CREATE FUNCTION public.record_fx_calendar_fetch(
  p_source TEXT, p_status TEXT, p_event_count INTEGER, p_covered_to TIMESTAMPTZ DEFAULT NULL,
  p_error TEXT DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_source IS NULL OR p_source !~ '^[a-z0-9-]{2,40}$'
     OR p_status IS NULL OR p_status NOT IN ('ok', 'error')
     OR p_event_count IS NULL OR p_event_count < 0
     OR (p_status = 'ok' AND (p_covered_to IS NULL OR NOT isfinite(p_covered_to)))
     OR (p_status = 'error' AND p_covered_to IS NOT NULL) THEN
    RAISE EXCEPTION 'Invalid fx calendar fetch';
  END IF;
  INSERT INTO public.fx_calendar_fetches (source, status, event_count, error_message, covered_to)
    VALUES (p_source, p_status, p_event_count, nullif(left(coalesce(p_error, ''), 1000), ''),
      p_covered_to);
  DELETE FROM public.fx_calendar_fetches WHERE fetched_at < clock_timestamp() - interval '90 days';
END;
$$;

-- Observations with event_ts in [p_from_ms, p_to_ms] (at most 120 days), and when p_as_of_ms is
-- given only those first seen by then. Each element is exactly a CalendarEvent of news_blackout.py,
-- ordered by (ts_ms, event_id). event_id is source:key:ts_ms, so a rescheduled event appears under its
-- own id with its own first_seen_ms. A re-rated or re-titled observation at the same time would
-- repeat that id (EventIndex rejects duplicates): every observation after the first of its
-- (source, key, time) gets the suffix ':o' || observation_id. The suffix is computed over all stored
-- observations of that time, so an id never depends on p_as_of_ms.
CREATE FUNCTION public.get_fx_calendar_events(
  p_from_ms BIGINT, p_to_ms BIGINT, p_as_of_ms BIGINT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  rows JSONB;
BEGIN
  IF p_from_ms IS NULL OR p_to_ms IS NULL OR p_to_ms < p_from_ms
     OR p_to_ms - p_from_ms > 120::BIGINT * 86400000 THEN
    RAISE EXCEPTION 'Invalid fx calendar request';
  END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_object(
      'event_id', e.event_id,
      'ts_ms', e.ts_ms,
      'currency', e.currency,
      'impact', e.impact,
      'title', e.title,
      'first_seen_ms', e.first_seen_ms,
      'source', e.source,
      'tags', '[]'::jsonb
    ) ORDER BY e.ts_ms, e.event_id), '[]'::jsonb)
    INTO rows
  FROM (
    SELECT o.source, o.currency, o.impact, o.title, o.first_seen_at,
      (extract(epoch FROM o.event_ts) * 1000)::BIGINT AS ts_ms,
      (extract(epoch FROM o.first_seen_at) * 1000)::BIGINT AS first_seen_ms,
      o.source || ':' || o.source_event_key || ':' || ((extract(epoch FROM o.event_ts) * 1000)::BIGINT)::TEXT
        || CASE WHEN row_number() OVER (
                  PARTITION BY o.source, o.source_event_key, o.event_ts ORDER BY o.observation_id
                ) = 1
             THEN '' ELSE ':o' || o.observation_id::TEXT END AS event_id
    FROM public.fx_calendar_observations o
    WHERE o.event_ts >= timestamptz 'epoch' + p_from_ms * interval '1 millisecond'
      AND o.event_ts <= timestamptz 'epoch' + p_to_ms * interval '1 millisecond'
  ) e
  WHERE p_as_of_ms IS NULL
     OR e.first_seen_at <= timestamptz 'epoch' + p_as_of_ms * interval '1 millisecond';
  RETURN rows;
END;
$$;

-- Per source from the fetch log: the latest ok fetch and how far it covered, the latest error.
-- NULL when there is none (the log keeps 90 days, so an older ok counts as never fetched).
CREATE FUNCTION public.get_fx_calendar_status()
RETURNS JSONB
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  SELECT coalesce(jsonb_agg(jsonb_build_object(
      'source', s.source,
      'last_ok_ms', (extract(epoch FROM ok.fetched_at) * 1000)::BIGINT,
      'last_ok_covered_to_ms', (extract(epoch FROM ok.covered_to) * 1000)::BIGINT,
      'last_error_ms', (extract(epoch FROM err.fetched_at) * 1000)::BIGINT,
      'last_error', err.error_message
    ) ORDER BY s.source), '[]'::jsonb)
  FROM (SELECT DISTINCT source FROM public.fx_calendar_fetches) s
  LEFT JOIN LATERAL (
    SELECT f.fetched_at, f.covered_to FROM public.fx_calendar_fetches f
    WHERE f.source = s.source AND f.status = 'ok'
    ORDER BY f.fetched_at DESC, f.fetch_id DESC LIMIT 1
  ) ok ON true
  LEFT JOIN LATERAL (
    SELECT f.fetched_at, f.error_message FROM public.fx_calendar_fetches f
    WHERE f.source = s.source AND f.status = 'error'
    ORDER BY f.fetched_at DESC, f.fetch_id DESC LIMIT 1
  ) err ON true;
$$;

REVOKE ALL ON FUNCTION
  public.record_fx_calendar(JSONB),
  public.record_fx_calendar_fetch(TEXT, TEXT, INTEGER, TIMESTAMPTZ, TEXT),
  public.get_fx_calendar_events(BIGINT, BIGINT, BIGINT),
  public.get_fx_calendar_status()
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION
  public.record_fx_calendar(JSONB),
  public.record_fx_calendar_fetch(TEXT, TEXT, INTEGER, TIMESTAMPTZ, TEXT),
  public.get_fx_calendar_events(BIGINT, BIGINT, BIGINT),
  public.get_fx_calendar_status()
  TO service_role;
