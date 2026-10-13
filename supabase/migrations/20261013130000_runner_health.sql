-- Global machine health contains only service states, never host identifiers or secrets.
CREATE TABLE public.runner_health (
  service TEXT PRIMARY KEY CHECK (service IN ('worker-host', 'runner-jobs')),
  status TEXT NOT NULL CHECK (status IN ('online', 'degraded', 'offline')),
  services JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(services) = 'object'),
  updated_at TIMESTAMPTZ NOT NULL,
  last_seen_at TIMESTAMPTZ
);

ALTER TABLE public.runner_health ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.runner_health FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.runner_health TO authenticated;
GRANT ALL ON public.runner_health TO service_role;

CREATE POLICY runner_health_select ON public.runner_health FOR SELECT TO authenticated
  USING (true);

CREATE FUNCTION public.report_runner_health(
  p_service TEXT, p_status TEXT, p_services JSONB DEFAULT '{}'::jsonb
)
RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
DECLARE
  observed_at TIMESTAMPTZ := clock_timestamp();
BEGIN
  IF p_service IS NULL OR p_service NOT IN ('worker-host', 'runner-jobs')
     OR p_status IS NULL OR p_status NOT IN ('online', 'degraded', 'offline')
     OR p_services IS NULL OR jsonb_typeof(p_services) <> 'object' THEN
    RAISE EXCEPTION 'Invalid runner health report';
  END IF;

  INSERT INTO public.runner_health (service, status, services, updated_at, last_seen_at)
    VALUES (
      p_service, p_status, p_services, observed_at,
      CASE WHEN p_status <> 'offline' THEN observed_at ELSE NULL END
    )
  ON CONFLICT (service) DO UPDATE SET
    status = EXCLUDED.status,
    services = EXCLUDED.services,
    updated_at = EXCLUDED.updated_at,
    last_seen_at = CASE WHEN EXCLUDED.status <> 'offline'
      THEN EXCLUDED.last_seen_at ELSE runner_health.last_seen_at END;
END;
$$;

REVOKE ALL ON FUNCTION public.report_runner_health(TEXT, TEXT, JSONB)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.report_runner_health(TEXT, TEXT, JSONB) TO service_role;
