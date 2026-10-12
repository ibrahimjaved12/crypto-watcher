-- Durable on-demand work in hosted main Supabase. Users enqueue/read; only the host owns execution.
CREATE TABLE public.runner_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('forward_signals', 'forward_trend')),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'expired')),
  idempotency_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  lease_expires_at TIMESTAMPTZ,
  worker_id TEXT,
  result JSONB,
  error TEXT,
  CONSTRAINT runner_jobs_idempotency_key UNIQUE (user_id, kind, idempotency_key)
);

CREATE UNIQUE INDEX runner_jobs_active_job ON public.runner_jobs (user_id, kind)
  WHERE status IN ('queued', 'running');

ALTER TABLE public.runner_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.runner_jobs FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON public.runner_jobs TO authenticated;
GRANT ALL ON public.runner_jobs TO service_role;

CREATE POLICY runner_jobs_select_own ON public.runner_jobs FOR SELECT TO authenticated
  USING (user_id = auth.uid());
CREATE POLICY runner_jobs_insert_own ON public.runner_jobs FOR INSERT TO authenticated
  WITH CHECK (
    user_id = auth.uid() AND status = 'queued' AND result IS NULL AND worker_id IS NULL
    AND started_at IS NULL AND finished_at IS NULL
  );
-- No user UPDATE or DELETE policies: status, ownership and results belong to the host.

CREATE FUNCTION public.claim_runner_job(p_worker_id TEXT, p_lease_seconds INT)
RETURNS SETOF public.runner_jobs
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0
     OR p_lease_seconds IS NULL OR p_lease_seconds <= 0 THEN
    RAISE EXCEPTION 'Invalid runner-job lease';
  END IF;

  UPDATE public.runner_jobs SET status = 'expired', finished_at = now()
    WHERE status = 'queued' AND created_at < now() - interval '15 minutes';
  UPDATE public.runner_jobs
    SET status = 'failed', error = 'Runner restarted before finishing', finished_at = now()
    WHERE status = 'running' AND lease_expires_at < now();

  RETURN QUERY
    UPDATE public.runner_jobs SET
      status = 'running', started_at = now(), worker_id = p_worker_id,
      lease_expires_at = now() + make_interval(secs => p_lease_seconds)
    WHERE id = (
      SELECT id FROM public.runner_jobs WHERE status = 'queued'
      ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
    )
    RETURNING *;
END;
$$;

CREATE FUNCTION public.extend_runner_job_lease(p_id UUID, p_worker_id TEXT, p_lease_seconds INT)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  IF p_lease_seconds IS NULL OR p_lease_seconds <= 0 THEN
    RAISE EXCEPTION 'Invalid runner-job lease';
  END IF;
  UPDATE public.runner_jobs SET lease_expires_at = now() + make_interval(secs => p_lease_seconds)
    WHERE id = p_id AND worker_id = p_worker_id AND status = 'running';
  RETURN FOUND;
END;
$$;

CREATE FUNCTION public.finish_runner_job(
  p_id UUID, p_worker_id TEXT, p_ok BOOLEAN, p_result JSONB, p_error TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  UPDATE public.runner_jobs SET
    status = CASE WHEN p_ok THEN 'succeeded' ELSE 'failed' END,
    result = CASE WHEN p_ok THEN p_result ELSE NULL END,
    error = CASE WHEN p_ok THEN NULL ELSE p_error END,
    finished_at = now(), lease_expires_at = NULL
    WHERE id = p_id AND worker_id = p_worker_id AND status = 'running';
  RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_runner_job(TEXT, INT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.extend_runner_job_lease(UUID, TEXT, INT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.finish_runner_job(UUID, TEXT, BOOLEAN, JSONB, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_runner_job(TEXT, INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.extend_runner_job_lease(UUID, TEXT, INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.finish_runner_job(UUID, TEXT, BOOLEAN, JSONB, TEXT) TO service_role;
