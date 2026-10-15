-- Alert setup charts follow-up (#299, #298). Operational database only; additive to
-- 20261016090000_alert_charts.sql (which is not edited).
--
-- 1. alert_chart_capture_failures: a failed or abandoned capture is recorded so it is visible in
--    mvp:check (the 8-day candle store means the window cannot be rebuilt later).
-- 2. alert_chart_event_ids: every event id that produced or repeated a chart. A pattern chart is
--    market-wide and stored under the first account's ta_signals.id; every other account's id for
--    the same candle is mapped here, so the --event-id re-run accepts any of them.
-- 3. requeue_alert_chart first applies the lease sweep (a rendering row whose lease elapsed becomes
--    failed) and reports rows whose lease is still valid instead of silently skipping them.
-- 4. get_alert_chart_diagnostics also counts capture failures.
-- The pending expiry default is now 7 days (an application setting passed to claim_alert_chart); the
-- window is stored, so a long wait loses nothing.

CREATE TABLE public.alert_chart_capture_failures (
  failure_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_kind TEXT NOT NULL CHECK (event_kind IN ('movement_alert', 'pattern')),
  event_id TEXT NOT NULL CHECK (length(event_id) BETWEEN 1 AND 128),
  symbol TEXT NOT NULL CHECK (symbol ~ '^[A-Z0-9]{2,30}$'),
  timeframe_minutes INTEGER NOT NULL CHECK (timeframe_minutes IN (1, 15, 60, 240)),
  event_candle_at TIMESTAMPTZ NOT NULL,
  error TEXT NOT NULL CHECK (length(error) BETWEEN 1 AND 500),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT alert_chart_capture_failures_event
    UNIQUE (event_kind, symbol, timeframe_minutes, event_candle_at)
);

COMMENT ON TABLE public.alert_chart_capture_failures IS
  'Alert/pattern events whose chart window could not be captured (read error after one retry, cap reached, or abandoned after the capture time cap). The candle store keeps ~8 days, so these windows are lost; this table makes the loss visible in mvp:check.';
COMMENT ON COLUMN public.alert_chart_capture_failures.event_id IS 'alerts.id or ta_signals.id of the event whose chart is missing.';
COMMENT ON COLUMN public.alert_chart_capture_failures.event_candle_at IS 'Open time of the event candle (the 1m alert candle for a movement alert).';
COMMENT ON COLUMN public.alert_chart_capture_failures.error IS 'Short error text (max 500 characters); never a stack or a secret.';

CREATE TABLE public.alert_chart_event_ids (
  event_id TEXT NOT NULL CHECK (length(event_id) BETWEEN 1 AND 128),
  chart_id UUID NOT NULL REFERENCES public.alert_charts (chart_id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (event_id, chart_id)
);

COMMENT ON TABLE public.alert_chart_event_ids IS
  'Every alerts.id / ta_signals.id that produced or repeated a chart, so a market-wide pattern chart can be found by any account''s signal id (--event-id).';

CREATE INDEX alert_chart_event_ids_chart ON public.alert_chart_event_ids (chart_id);

INSERT INTO public.alert_chart_event_ids (event_id, chart_id)
  SELECT event_id, chart_id FROM public.alert_charts ON CONFLICT DO NOTHING;

ALTER TABLE public.alert_chart_capture_failures ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alert_chart_event_ids ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.alert_chart_capture_failures FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.alert_chart_event_ids FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.alert_chart_capture_failures TO service_role;
GRANT SELECT ON public.alert_chart_event_ids TO service_role;

COMMENT ON COLUMN public.alert_charts.status IS
  'pending -> rendering -> rendered | failed; pending -> expired after ALERT_CHART_PENDING_EXPIRY_MINUTES (default 10080 = 7 days; the window is stored, so a long wait loses nothing). Requeue sets pending again.';

-- enqueue_alert_chart: same behaviour as before, plus the event id is mapped on every attempt,
-- including repeated and concurrent captures of the same event.
CREATE OR REPLACE FUNCTION public.enqueue_alert_chart(
  p_event_kind TEXT, p_event_id TEXT, p_user_id UUID, p_symbol TEXT, p_timeframe_minutes INTEGER,
  p_event_candle_at TIMESTAMPTZ, p_rule_version TEXT, p_scope_key TEXT, p_window_version TEXT,
  p_window JSONB, p_max_rows BIGINT, p_max_bytes BIGINT
)
RETURNS TABLE (chart_id UUID, created BOOLEAN)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
#variable_conflict use_column
DECLARE
  event_ms BIGINT := (extract(epoch FROM p_event_candle_at) * 1000)::BIGINT;
  existing UUID;
  used_rows BIGINT;
  used_bytes BIGINT;
  saved UUID;
BEGIN
  IF p_max_rows IS NULL OR p_max_rows < 1 OR p_max_bytes IS NULL OR p_max_bytes < 1 THEN
    RAISE EXCEPTION 'Invalid alert chart cap';
  END IF;
  IF p_window IS NULL OR coalesce(jsonb_typeof(p_window->'candles'), '') <> 'array'
     OR jsonb_array_length(p_window->'candles') = 0 THEN
    RAISE EXCEPTION 'Alert chart window has no candles';
  END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(p_window->'candles') AS c(candle)
    WHERE jsonb_typeof(c.candle) <> 'array' OR jsonb_array_length(c.candle) <> 6
       OR (c.candle->>0)::BIGINT > event_ms
  ) THEN
    RAISE EXCEPTION 'Alert chart window is not point-in-time';
  END IF;

  SELECT a.chart_id INTO existing FROM public.alert_charts a
    WHERE a.event_kind = p_event_kind AND a.symbol = p_symbol
      AND a.timeframe_minutes = p_timeframe_minutes AND a.event_candle_at = p_event_candle_at
      AND a.rule_version = p_rule_version AND a.scope_key = coalesce(p_scope_key, '');
  IF FOUND THEN
    INSERT INTO public.alert_chart_event_ids (event_id, chart_id) VALUES (p_event_id, existing)
      ON CONFLICT DO NOTHING;
    RETURN QUERY SELECT existing, false;
    RETURN;
  END IF;

  SELECT count(*), coalesce(sum(a.window_bytes::BIGINT + coalesce(a.png_bytes, 0)), 0)
    INTO used_rows, used_bytes FROM public.alert_charts a;
  IF used_rows >= p_max_rows OR used_bytes >= p_max_bytes THEN
    RAISE EXCEPTION 'Alert chart cap reached (% rows, % bytes); nothing is deleted automatically',
      used_rows, used_bytes;
  END IF;

  INSERT INTO public.alert_charts AS a (
    event_kind, event_id, user_id, symbol, timeframe_minutes, event_candle_at, rule_version,
    scope_key, window_version, candle_window, window_bytes
  ) VALUES (
    p_event_kind, p_event_id, p_user_id, p_symbol, p_timeframe_minutes, p_event_candle_at,
    p_rule_version, coalesce(p_scope_key, ''), p_window_version, p_window,
    octet_length(p_window::TEXT)
  )
  ON CONFLICT ON CONSTRAINT alert_charts_event_identity DO NOTHING
  RETURNING a.chart_id INTO saved;
  IF saved IS NULL THEN  -- a concurrent capture of the same event won
    SELECT a.chart_id INTO saved FROM public.alert_charts a
      WHERE a.event_kind = p_event_kind AND a.symbol = p_symbol
        AND a.timeframe_minutes = p_timeframe_minutes AND a.event_candle_at = p_event_candle_at
        AND a.rule_version = p_rule_version AND a.scope_key = coalesce(p_scope_key, '');
    INSERT INTO public.alert_chart_event_ids (event_id, chart_id) VALUES (p_event_id, saved)
      ON CONFLICT DO NOTHING;
    RETURN QUERY SELECT saved, false;
    RETURN;
  END IF;
  INSERT INTO public.alert_chart_event_ids (event_id, chart_id) VALUES (p_event_id, saved)
    ON CONFLICT DO NOTHING;
  RETURN QUERY SELECT saved, true;
END;
$$;

-- Record a capture that failed or was abandoned. Idempotent on the event identity (the first error is kept).
CREATE FUNCTION public.record_alert_chart_capture_failure(
  p_event_kind TEXT, p_event_id TEXT, p_symbol TEXT, p_timeframe_minutes INTEGER,
  p_event_candle_at TIMESTAMPTZ, p_error TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  INSERT INTO public.alert_chart_capture_failures
    (event_kind, event_id, symbol, timeframe_minutes, event_candle_at, error)
  VALUES (p_event_kind, p_event_id, p_symbol, p_timeframe_minutes, p_event_candle_at,
          left(coalesce(nullif(trim(p_error), ''), 'Capture failed'), 500))
  ON CONFLICT ON CONSTRAINT alert_chart_capture_failures_event DO NOTHING;
  RETURN FOUND;
END;
$$;

-- Command-line re-run by event id (alert_charts.event_id or any mapped id). Sweeps stale leases first,
-- requeues every non-rendering chart of the event and reports charts that are rendering with a valid lease.
DROP FUNCTION public.requeue_alert_chart(TEXT);
CREATE FUNCTION public.requeue_alert_chart(p_event_id TEXT)
RETURNS TABLE (chart_id UUID, outcome TEXT, lease_expires_at TIMESTAMPTZ)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
#variable_conflict use_column
BEGIN
  UPDATE public.alert_charts a
    SET status = 'failed', error = 'Runner restarted before finishing', finished_at = now(),
        lease_expires_at = NULL
    WHERE a.status = 'rendering' AND a.lease_expires_at < now();

  RETURN QUERY
    WITH matched AS (
      SELECT a.chart_id FROM public.alert_charts a WHERE a.event_id = p_event_id
      UNION
      SELECT m.chart_id FROM public.alert_chart_event_ids m WHERE m.event_id = p_event_id
    ), requeued AS (
      UPDATE public.alert_charts a SET status = 'pending', queued_at = now(), error = NULL,
        worker_id = NULL, lease_expires_at = NULL, finished_at = NULL
      WHERE a.chart_id IN (SELECT chart_id FROM matched) AND a.status <> 'rendering'
      RETURNING a.chart_id
    )
    SELECT r.chart_id, 'requeued'::TEXT, NULL::TIMESTAMPTZ FROM requeued r
    UNION ALL
    SELECT a.chart_id, 'rendering'::TEXT, a.lease_expires_at FROM public.alert_charts a
      WHERE a.chart_id IN (SELECT chart_id FROM matched) AND a.status = 'rendering';
END;
$$;

DROP FUNCTION public.get_alert_chart_diagnostics();
CREATE FUNCTION public.get_alert_chart_diagnostics()
RETURNS TABLE (
  chart_rows BIGINT, window_bytes BIGINT, png_bytes BIGINT, pending_rows BIGINT,
  rendering_rows BIGINT, rendered_rows BIGINT, failed_rows BIGINT, expired_rows BIGINT,
  oldest_pending_at TIMESTAMPTZ, newest_created_at TIMESTAMPTZ,
  capture_failures_24h BIGINT, capture_failures_total BIGINT
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
AS $$
  SELECT count(*), coalesce(sum(window_bytes), 0)::BIGINT, coalesce(sum(png_bytes), 0)::BIGINT,
    count(*) FILTER (WHERE status = 'pending'), count(*) FILTER (WHERE status = 'rendering'),
    count(*) FILTER (WHERE status = 'rendered'), count(*) FILTER (WHERE status = 'failed'),
    count(*) FILTER (WHERE status = 'expired'),
    min(queued_at) FILTER (WHERE status = 'pending'), max(created_at),
    (SELECT count(*) FROM public.alert_chart_capture_failures WHERE created_at > now() - interval '24 hours'),
    (SELECT count(*) FROM public.alert_chart_capture_failures)
  FROM public.alert_charts;
$$;

REVOKE ALL ON FUNCTION public.record_alert_chart_capture_failure(TEXT, TEXT, TEXT, INTEGER, TIMESTAMPTZ, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.requeue_alert_chart(TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.get_alert_chart_diagnostics() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.record_alert_chart_capture_failure(TEXT, TEXT, TEXT, INTEGER, TIMESTAMPTZ, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.requeue_alert_chart(TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_alert_chart_diagnostics() TO service_role;
