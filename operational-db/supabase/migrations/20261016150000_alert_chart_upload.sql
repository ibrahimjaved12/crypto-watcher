-- Hosted copy of movement-alert charts (#298): the machine uploads each rendered movement-alert PNG
-- to the owner's private hosted bucket. This migration only adds the machine-side upload state to
-- alert_charts; the local PNG is never touched, moved or deleted by an upload, so an offline machine
-- or a failed upload loses nothing. Additive to 20261016090000 and 20261016120000 (not edited).

ALTER TABLE public.alert_charts
  ADD COLUMN hosted_status TEXT NOT NULL DEFAULT 'not_applicable'
    CHECK (hosted_status IN ('not_applicable', 'pending', 'uploaded', 'refused')),
  ADD COLUMN upload_attempts INTEGER NOT NULL DEFAULT 0 CHECK (upload_attempts >= 0),
  ADD COLUMN upload_next_at TIMESTAMPTZ,
  ADD COLUMN upload_error TEXT CHECK (upload_error IS NULL OR length(upload_error) <= 300),
  ADD COLUMN uploaded_at TIMESTAMPTZ;

COMMENT ON COLUMN public.alert_charts.hosted_status IS
  'Hosted copy of a movement_alert chart: not_applicable (pattern charts, or hosted upload off), pending (rendered, to be uploaded or retried), uploaded, refused (permanent: the alert was deleted or the hosted cap refused it). Re-queueing an event queues a new upload.';
COMMENT ON COLUMN public.alert_charts.upload_attempts IS 'Failed upload attempts since the last queueing; drives the backoff.';
COMMENT ON COLUMN public.alert_charts.upload_next_at IS 'Earliest time of the next upload attempt (backoff: 1 min doubling to 1 h); also the short lease of a running attempt.';

CREATE INDEX alert_charts_upload_due ON public.alert_charts (upload_next_at) WHERE hosted_status = 'pending';

-- Queue the upload of a rendered movement-alert chart (the consumer calls it only when hosted upload is configured).
CREATE FUNCTION public.queue_alert_chart_upload(p_chart_id UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  UPDATE public.alert_charts SET hosted_status = 'pending', upload_attempts = 0, upload_next_at = now(),
      upload_error = NULL
    WHERE chart_id = p_chart_id AND event_kind = 'movement_alert' AND status = 'rendered';
  RETURN FOUND;
END;
$$;

-- Claim one due upload; the 2-minute push of upload_next_at is its lease (a crash retries after it).
CREATE FUNCTION public.claim_alert_chart_upload(p_chart_id UUID DEFAULT NULL)
RETURNS SETOF public.alert_charts
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  RETURN QUERY
    UPDATE public.alert_charts SET upload_next_at = now() + interval '2 minutes'
    WHERE chart_id = (
      SELECT a.chart_id FROM public.alert_charts a
      WHERE a.hosted_status = 'pending' AND a.status = 'rendered' AND a.upload_next_at <= now()
        AND (p_chart_id IS NULL OR a.chart_id = p_chart_id)
      ORDER BY a.upload_next_at, a.chart_id LIMIT 1 FOR UPDATE SKIP LOCKED
    )
    RETURNING *;
END;
$$;

-- Record an attempt: success, a transient failure (backoff, retried forever) or a permanent refusal.
CREATE FUNCTION public.finish_alert_chart_upload(p_chart_id UUID, p_ok BOOLEAN, p_permanent BOOLEAN, p_error TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  UPDATE public.alert_charts SET
    hosted_status = CASE WHEN p_ok THEN 'uploaded' WHEN p_permanent THEN 'refused' ELSE 'pending' END,
    uploaded_at = CASE WHEN p_ok THEN now() ELSE uploaded_at END,
    upload_attempts = CASE WHEN p_ok OR p_permanent THEN upload_attempts ELSE upload_attempts + 1 END,
    upload_next_at = CASE WHEN p_ok OR p_permanent THEN NULL
      ELSE now() + make_interval(secs => least(3600, 60 * power(2, least(upload_attempts, 6)))::INTEGER) END,
    upload_error = CASE WHEN p_ok THEN NULL ELSE left(coalesce(p_error, 'Upload failed'), 300) END
    WHERE chart_id = p_chart_id AND hosted_status = 'pending';
  RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION public.queue_alert_chart_upload(UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.claim_alert_chart_upload(UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.finish_alert_chart_upload(UUID, BOOLEAN, BOOLEAN, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.queue_alert_chart_upload(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_alert_chart_upload(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.finish_alert_chart_upload(UUID, BOOLEAN, BOOLEAN, TEXT) TO service_role;
