-- Lets a signed-in user expire their own stale queued runner jobs so an offline host
-- never blocks new requests (#294). Queued work older than 15 minutes is never run.
CREATE OR REPLACE FUNCTION public.expire_my_stale_runner_jobs()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
  changed integer;
BEGIN
  IF auth.uid() IS NULL THEN
    RETURN 0;
  END IF;
  UPDATE public.runner_jobs
     SET status = 'expired', finished_at = now()
   WHERE user_id = auth.uid() AND status = 'queued'
     AND created_at < now() - interval '15 minutes';
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed;
END;
$$;

REVOKE ALL ON FUNCTION public.expire_my_stale_runner_jobs() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.expire_my_stale_runner_jobs() TO authenticated, service_role;
