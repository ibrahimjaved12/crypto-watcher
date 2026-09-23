-- Durable due-work and measurement primitives shared by the current REST runner
-- and the future streaming collector. Raw market events remain outside PostgreSQL.
ALTER TABLE public.monitor_runs
  ADD COLUMN duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  ADD COLUMN metrics JSONB NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(metrics) = 'object');

CREATE INDEX ta_signals_latest_version
  ON public.ta_signals(user_id, symbol, version, timeframe, candle_at DESC);

-- Return one latest checkpoint per TA frame plus only outcomes whose evaluation
-- time has arrived. This replaces repeated broad pending-history reads.
CREATE FUNCTION public.get_ta_due_work(
  p_user_id UUID,
  p_symbol TEXT,
  p_version TEXT,
  p_now TIMESTAMPTZ,
  p_include_generation BOOLEAN,
  p_include_outcomes BOOLEAN
) RETURNS TABLE (
  work_kind TEXT,
  id UUID,
  timeframe INTEGER,
  candle_at TIMESTAMPTZ,
  source TEXT,
  detected_at TIMESTAMPTZ,
  price DOUBLE PRECISION
)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  WITH latest AS (
    SELECT DISTINCT ON (s.timeframe)
      'latest'::TEXT AS work_kind,
      s.id,
      s.timeframe,
      s.candle_at,
      s.source,
      s.detected_at,
      s.price
    FROM public.ta_signals s
    WHERE p_include_generation
      AND s.user_id = p_user_id
      AND s.symbol = p_symbol
      AND s.version = p_version
    ORDER BY s.timeframe, s.candle_at DESC
  ), due_ranked AS (
    SELECT
      'outcome'::TEXT AS work_kind,
      s.id,
      s.timeframe,
      s.candle_at,
      s.source,
      s.detected_at,
      s.price,
      row_number() OVER (PARTITION BY s.timeframe ORDER BY s.detected_at) AS outcome_rank
    FROM public.ta_signals s
    WHERE p_include_outcomes
      AND s.user_id = p_user_id
      AND s.symbol = p_symbol
      AND s.outcome_status = 'pending'
      AND to_timestamp(
        ceil(extract(epoch FROM s.detected_at) / (s.timeframe * 60)) * (s.timeframe * 60)
        + 4 * s.timeframe * 60
      ) <= p_now
  ), due_outcomes AS (
    SELECT work_kind, id, timeframe, candle_at, source, detected_at, price
    FROM due_ranked
    WHERE outcome_rank <= 500
  )
  SELECT * FROM latest
  UNION ALL
  SELECT * FROM due_outcomes;
$$;

REVOKE ALL ON FUNCTION public.get_ta_due_work(
  UUID, TEXT, TEXT, TIMESTAMPTZ, BOOLEAN, BOOLEAN
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_ta_due_work(
  UUID, TEXT, TEXT, TIMESTAMPTZ, BOOLEAN, BOOLEAN
) TO service_role;

-- Apply compatible outcome transitions in one transaction and retain the
-- pending-state predicate so retries and overlapping workers are idempotent.
CREATE FUNCTION public.apply_ta_outcomes(
  p_user_id UUID,
  p_outcomes JSONB
) RETURNS INTEGER
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  changed_rows INTEGER;
BEGIN
  IF jsonb_typeof(p_outcomes) <> 'array'
     OR jsonb_array_length(p_outcomes) > 500 THEN
    RAISE EXCEPTION 'Invalid TA outcome batch';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM jsonb_to_recordset(p_outcomes) AS x(
      id UUID,
      outcome_status TEXT,
      outcome_at TIMESTAMPTZ,
      outcome_price DOUBLE PRECISION,
      return_pct DOUBLE PRECISION
    )
    WHERE x.id IS NULL
       OR x.outcome_status NOT IN ('measured', 'unavailable')
       OR (x.outcome_status = 'measured' AND (
         x.outcome_at IS NULL OR NOT isfinite(x.outcome_at)
         OR x.outcome_price IS NULL OR NOT (x.outcome_price > 0 AND x.outcome_price < 'Infinity'::float8)
         OR x.return_pct IS NULL OR NOT (
           x.return_pct > '-Infinity'::float8 AND x.return_pct < 'Infinity'::float8
         )
       ))
       OR (x.outcome_status = 'unavailable' AND (
         x.outcome_at IS NOT NULL OR x.outcome_price IS NOT NULL OR x.return_pct IS NOT NULL
       ))
  ) THEN
    RAISE EXCEPTION 'Invalid TA outcome transition';
  END IF;

  IF (
    SELECT count(*) <> count(DISTINCT x.id)
    FROM jsonb_to_recordset(p_outcomes) AS x(id UUID)
  ) THEN
    RAISE EXCEPTION 'Duplicate TA outcome transition';
  END IF;

  WITH supplied AS (
    SELECT *
    FROM jsonb_to_recordset(p_outcomes) AS x(
      id UUID,
      outcome_status TEXT,
      outcome_at TIMESTAMPTZ,
      outcome_price DOUBLE PRECISION,
      return_pct DOUBLE PRECISION
    )
  )
  UPDATE public.ta_signals AS signal
  SET outcome_status = supplied.outcome_status,
      outcome_at = supplied.outcome_at,
      outcome_price = supplied.outcome_price,
      return_pct = supplied.return_pct
  FROM supplied
  WHERE signal.id = supplied.id
    AND signal.user_id = p_user_id
    AND signal.outcome_status = 'pending';

  GET DIAGNOSTICS changed_rows = ROW_COUNT;
  RETURN changed_rows;
END;
$$;

REVOKE ALL ON FUNCTION public.apply_ta_outcomes(UUID, JSONB)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.apply_ta_outcomes(UUID, JSONB)
  TO service_role;

-- Operational run history is intentionally bounded per account. Immutable alerts
-- and TA research history are not pruned by this function.
CREATE FUNCTION public.record_monitor_run(
  p_user_id UUID,
  p_status TEXT,
  p_symbols_checked INTEGER,
  p_alerts_created INTEGER,
  p_data_source TEXT,
  p_error_message TEXT,
  p_duration_ms INTEGER,
  p_metrics JSONB
) RETURNS UUID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  saved_id UUID;
BEGIN
  IF p_status NOT IN ('success', 'partial', 'failed')
     OR p_symbols_checked < 0 OR p_alerts_created < 0
     OR p_duration_ms < 0
     OR jsonb_typeof(p_metrics) <> 'object' THEN
    RAISE EXCEPTION 'Invalid monitor run';
  END IF;

  INSERT INTO public.monitor_runs (
    user_id, status, symbols_checked, alerts_created, data_source,
    error_message, duration_ms, metrics
  ) VALUES (
    p_user_id, p_status, p_symbols_checked, p_alerts_created, p_data_source,
    p_error_message, p_duration_ms, p_metrics
  ) RETURNING id INTO saved_id;

  DELETE FROM public.monitor_runs
  WHERE id IN (
    SELECT id
    FROM public.monitor_runs
    WHERE user_id IS NOT DISTINCT FROM p_user_id
    ORDER BY ran_at DESC, id DESC
    OFFSET 1000
  );

  RETURN saved_id;
END;
$$;

REVOKE ALL ON FUNCTION public.record_monitor_run(
  UUID, TEXT, INTEGER, INTEGER, TEXT, TEXT, INTEGER, JSONB
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.record_monitor_run(
  UUID, TEXT, INTEGER, INTEGER, TEXT, TEXT, INTEGER, JSONB
) TO service_role;
