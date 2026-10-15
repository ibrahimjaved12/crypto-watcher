-- Alert setup charts (#299, #298; owner decision 2026-10-16). Operational database only.
--
-- When a movement alert is saved or a TA pattern event is written, the application copies a
-- point-in-time candle window here (the operational candle store keeps only ~8 days, so the window
-- must be copied at event time) and the row doubles as the render job for the machine-side
-- consumer (worker-host child `alert-charts`). The PNG is written to ALERT_CHART_DIR on the
-- machine's disk, never into the database; the stored window can regenerate it.
--
-- Job rules follow public.runner_jobs (#294): one claim at a time with a lease, a running row
-- whose lease elapsed becomes failed ('Runner restarted before finishing') and is not requeued
-- automatically, pending rows older than the expiry become expired and are never replayed
-- automatically. Any row can be requeued explicitly by event id from the command line.
-- Retention: a row/byte cap passed by the application; at the cap new captures are refused
-- visibly. Nothing here ever deletes a row.

CREATE TABLE public.alert_charts (
  chart_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_kind TEXT NOT NULL CHECK (event_kind IN ('movement_alert', 'pattern')),
  event_id TEXT NOT NULL CHECK (length(event_id) BETWEEN 1 AND 128),
  user_id UUID,
  symbol TEXT NOT NULL CHECK (symbol ~ '^[A-Z0-9]{2,30}$'),
  timeframe_minutes INTEGER NOT NULL CHECK (timeframe_minutes IN (1, 15, 60, 240)),
  event_candle_at TIMESTAMPTZ NOT NULL,
  rule_version TEXT NOT NULL CHECK (length(rule_version) BETWEEN 1 AND 128),
  scope_key TEXT NOT NULL DEFAULT '' CHECK (length(scope_key) <= 64),
  window_version TEXT NOT NULL CHECK (length(window_version) BETWEEN 1 AND 64),
  candle_window JSONB NOT NULL,
  window_bytes INTEGER NOT NULL CHECK (window_bytes > 0),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'rendering', 'rendered', 'failed', 'expired')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  error TEXT CHECK (error IS NULL OR length(error) <= 500),
  worker_id TEXT,
  lease_expires_at TIMESTAMPTZ,
  png_path TEXT CHECK (png_path IS NULL OR length(png_path) <= 1024),
  png_sha256 TEXT CHECK (png_sha256 IS NULL OR png_sha256 ~ '^[0-9a-f]{64}$'),
  png_bytes INTEGER CHECK (png_bytes IS NULL OR png_bytes > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  rendered_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  CONSTRAINT alert_charts_event_identity
    UNIQUE (event_kind, symbol, timeframe_minutes, event_candle_at, rule_version, scope_key),
  CONSTRAINT alert_charts_rendered_has_png CHECK (
    status <> 'rendered' OR (png_path IS NOT NULL AND png_sha256 IS NOT NULL AND png_bytes IS NOT NULL)
  )
);

COMMENT ON TABLE public.alert_charts IS
  'Point-in-time candle windows captured at alert/pattern event time and their machine-side render job (#299). PNGs live on disk under ALERT_CHART_DIR, never here.';
COMMENT ON COLUMN public.alert_charts.event_kind IS 'movement_alert (main-database alerts row) or pattern (main-database ta_signals row with patterns).';
COMMENT ON COLUMN public.alert_charts.event_id IS 'Id of the main-database row that created the event (alerts.id or the first ta_signals.id). Informational; the identity is the unique key.';
COMMENT ON COLUMN public.alert_charts.user_id IS 'Owner of a movement alert (its baseline and threshold are per account); NULL for market-wide pattern events.';
COMMENT ON COLUMN public.alert_charts.timeframe_minutes IS 'Timeframe of the event candle: 1 for a movement alert (the completed 1m observation), 15/60/240 for a pattern. The window timeframe is inside the window.';
COMMENT ON COLUMN public.alert_charts.event_candle_at IS 'Open time of the event candle. The window holds no candle that opens after it.';
COMMENT ON COLUMN public.alert_charts.rule_version IS 'Detection rule version: cumulative-baseline-v1 for movement alerts, the TA version (detect_patterns) for patterns.';
COMMENT ON COLUMN public.alert_charts.scope_key IS 'Identity scope: the user id for movement alerts (per-account baseline), empty for market-wide pattern events.';
COMMENT ON COLUMN public.alert_charts.candle_window IS 'Compact window JSON (alert-chart-window-v1): candles as [open_ms, open, high, low, close, volume] arrays plus event fields. Enough to regenerate the PNG.';
COMMENT ON COLUMN public.alert_charts.window_bytes IS 'octet_length of the stored window text, for the retention cap and mvp:check.';
COMMENT ON COLUMN public.alert_charts.status IS 'pending -> rendering -> rendered | failed; pending -> expired after the expiry window. Requeue sets pending again.';
COMMENT ON COLUMN public.alert_charts.queued_at IS 'Start of the current pending period (created or requeued); the expiry sweep measures from here.';
COMMENT ON COLUMN public.alert_charts.png_path IS 'Absolute path of the rendered PNG on the machine''s disk.';

CREATE INDEX alert_charts_pending ON public.alert_charts (queued_at, chart_id) WHERE status = 'pending';
CREATE INDEX alert_charts_event_id ON public.alert_charts (event_id);

ALTER TABLE public.alert_charts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.alert_charts FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.alert_charts TO service_role;
-- No policies and no direct writes: every change goes through the functions below.

-- Insert a captured window once. Returns the existing row (created = false) for a repeated event.
-- Refuses a window with a candle that opens after the event candle (point-in-time guard) and a new
-- row once the row or byte cap is reached (never deletes to make room).
CREATE FUNCTION public.enqueue_alert_chart(
  p_event_kind TEXT, p_event_id TEXT, p_user_id UUID, p_symbol TEXT, p_timeframe_minutes INTEGER,
  p_event_candle_at TIMESTAMPTZ, p_rule_version TEXT, p_scope_key TEXT, p_window_version TEXT,
  p_window JSONB, p_max_rows BIGINT, p_max_bytes BIGINT
)
RETURNS TABLE (chart_id UUID, created BOOLEAN)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
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
    RETURN QUERY SELECT saved, false;
    RETURN;
  END IF;
  RETURN QUERY SELECT saved, true;
END;
$$;

-- Sweep, then claim one pending row (the oldest, or p_chart_id when given). Same rules as
-- claim_runner_job: stale pending rows expire, rows whose lease elapsed fail.
CREATE FUNCTION public.claim_alert_chart(
  p_worker_id TEXT, p_lease_seconds INTEGER, p_expiry_minutes INTEGER, p_chart_id UUID DEFAULT NULL
)
RETURNS SETOF public.alert_charts
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0
     OR p_lease_seconds IS NULL OR p_lease_seconds <= 0
     OR p_expiry_minutes IS NULL OR p_expiry_minutes <= 0 THEN
    RAISE EXCEPTION 'Invalid alert chart lease';
  END IF;

  UPDATE public.alert_charts SET status = 'expired', finished_at = now()
    WHERE status = 'pending' AND queued_at < now() - make_interval(mins => p_expiry_minutes)
      AND (p_chart_id IS NULL OR chart_id <> p_chart_id);
  UPDATE public.alert_charts
    SET status = 'failed', error = 'Runner restarted before finishing', finished_at = now(),
        lease_expires_at = NULL
    WHERE status = 'rendering' AND lease_expires_at < now();

  RETURN QUERY
    UPDATE public.alert_charts SET
      status = 'rendering', started_at = now(), worker_id = p_worker_id, attempts = attempts + 1,
      error = NULL, lease_expires_at = now() + make_interval(secs => p_lease_seconds)
    WHERE chart_id = (
      SELECT a.chart_id FROM public.alert_charts a
      WHERE a.status = 'pending' AND (p_chart_id IS NULL OR a.chart_id = p_chart_id)
      ORDER BY a.queued_at, a.chart_id LIMIT 1 FOR UPDATE SKIP LOCKED
    )
    RETURNING *;
END;
$$;

CREATE FUNCTION public.finish_alert_chart(
  p_chart_id UUID, p_worker_id TEXT, p_ok BOOLEAN, p_png_path TEXT, p_png_sha256 TEXT,
  p_png_bytes INTEGER, p_error TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  UPDATE public.alert_charts SET
    status = CASE WHEN p_ok THEN 'rendered' ELSE 'failed' END,
    png_path = CASE WHEN p_ok THEN p_png_path ELSE png_path END,
    png_sha256 = CASE WHEN p_ok THEN p_png_sha256 ELSE png_sha256 END,
    png_bytes = CASE WHEN p_ok THEN p_png_bytes ELSE png_bytes END,
    rendered_at = CASE WHEN p_ok THEN now() ELSE rendered_at END,
    error = CASE WHEN p_ok THEN NULL ELSE left(coalesce(p_error, 'Render failed'), 500) END,
    finished_at = now(), lease_expires_at = NULL
    WHERE chart_id = p_chart_id AND worker_id = p_worker_id AND status = 'rendering';
  RETURN FOUND;
END;
$$;

-- Command-line re-run: put every non-rendering chart of one event id back to pending.
CREATE FUNCTION public.requeue_alert_chart(p_event_id TEXT)
RETURNS SETOF UUID
LANGUAGE sql SECURITY DEFINER SET search_path = public
AS $$
  UPDATE public.alert_charts SET status = 'pending', queued_at = now(), error = NULL,
    worker_id = NULL, lease_expires_at = NULL, finished_at = NULL
  WHERE event_id = p_event_id AND status <> 'rendering'
  RETURNING chart_id;
$$;

CREATE FUNCTION public.get_alert_chart_diagnostics()
RETURNS TABLE (
  chart_rows BIGINT, window_bytes BIGINT, png_bytes BIGINT, pending_rows BIGINT,
  rendering_rows BIGINT, rendered_rows BIGINT, failed_rows BIGINT, expired_rows BIGINT,
  oldest_pending_at TIMESTAMPTZ, newest_created_at TIMESTAMPTZ
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
AS $$
  SELECT count(*), coalesce(sum(window_bytes), 0)::BIGINT, coalesce(sum(png_bytes), 0)::BIGINT,
    count(*) FILTER (WHERE status = 'pending'), count(*) FILTER (WHERE status = 'rendering'),
    count(*) FILTER (WHERE status = 'rendered'), count(*) FILTER (WHERE status = 'failed'),
    count(*) FILTER (WHERE status = 'expired'),
    min(queued_at) FILTER (WHERE status = 'pending'), max(created_at)
  FROM public.alert_charts;
$$;

REVOKE ALL ON FUNCTION public.enqueue_alert_chart(TEXT, TEXT, UUID, TEXT, INTEGER, TIMESTAMPTZ, TEXT, TEXT, TEXT, JSONB, BIGINT, BIGINT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.claim_alert_chart(TEXT, INTEGER, INTEGER, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.finish_alert_chart(UUID, TEXT, BOOLEAN, TEXT, TEXT, INTEGER, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.requeue_alert_chart(TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.get_alert_chart_diagnostics() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.enqueue_alert_chart(TEXT, TEXT, UUID, TEXT, INTEGER, TIMESTAMPTZ, TEXT, TEXT, TEXT, JSONB, BIGINT, BIGINT) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_alert_chart(TEXT, INTEGER, INTEGER, UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.finish_alert_chart(UUID, TEXT, BOOLEAN, TEXT, TEXT, INTEGER, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.requeue_alert_chart(TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_alert_chart_diagnostics() TO service_role;
