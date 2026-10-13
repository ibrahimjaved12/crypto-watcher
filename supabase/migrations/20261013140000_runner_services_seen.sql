-- Service history is shared machine state; timestamps come only from the database clock.
ALTER TABLE public.runner_health
  ADD COLUMN services_seen JSONB NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(services_seen) = 'object');

-- CREATE OR REPLACE preserves the existing execution grants.
CREATE OR REPLACE FUNCTION public.report_runner_health(
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

  INSERT INTO public.runner_health (
    service, status, services, updated_at, last_seen_at, services_seen
  )
    VALUES (
      p_service, p_status, p_services, observed_at,
      CASE WHEN p_status <> 'offline' THEN observed_at ELSE NULL END,
      COALESCE(NULL::jsonb, '{}'::jsonb) || (
        SELECT COALESCE(jsonb_object_agg(key, to_jsonb(observed_at)), '{}'::jsonb)
        FROM jsonb_each(p_services) WHERE value = 'true'::jsonb
      )
    )
  ON CONFLICT (service) DO UPDATE SET
    status = EXCLUDED.status,
    services = EXCLUDED.services,
    updated_at = EXCLUDED.updated_at,
    last_seen_at = CASE WHEN EXCLUDED.status <> 'offline'
      THEN EXCLUDED.last_seen_at ELSE runner_health.last_seen_at END,
    services_seen = COALESCE(runner_health.services_seen, '{}'::jsonb) || (
      SELECT COALESCE(jsonb_object_agg(key, to_jsonb(observed_at)), '{}'::jsonb)
      FROM jsonb_each(p_services) WHERE value = 'true'::jsonb
    );
END;
$$;
