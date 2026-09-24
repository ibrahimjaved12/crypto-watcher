-- One renewable monitor batch per user across manual and scheduled entrypoints.
-- Lovable remains the authority because every monitor run already depends on
-- Lovable-owned user, watchlist, movement, and TA state.
CREATE TABLE public.monitor_run_leases (
  user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  owner_id UUID NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  leased_until TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CHECK (leased_until > acquired_at)
);

ALTER TABLE public.monitor_run_leases ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.monitor_run_leases FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.monitor_run_leases TO service_role;

CREATE FUNCTION public.claim_monitor_run_lease(
  p_user_id UUID, p_owner_id UUID, p_lease_seconds INTEGER DEFAULT 180
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_user_id IS NULL OR p_owner_id IS NULL
     OR p_lease_seconds < 30 OR p_lease_seconds > 900 THEN
    RAISE EXCEPTION 'Invalid monitor-run lease';
  END IF;

  INSERT INTO public.monitor_run_leases(
    user_id, owner_id, acquired_at, leased_until, updated_at
  ) VALUES (
    p_user_id, p_owner_id, clock_timestamp(),
    clock_timestamp() + make_interval(secs => p_lease_seconds), clock_timestamp()
  )
  ON CONFLICT (user_id) DO UPDATE SET
    owner_id = EXCLUDED.owner_id,
    acquired_at = EXCLUDED.acquired_at,
    leased_until = EXCLUDED.leased_until,
    updated_at = clock_timestamp()
  WHERE public.monitor_run_leases.leased_until < clock_timestamp()
     OR public.monitor_run_leases.owner_id = EXCLUDED.owner_id;

  RETURN FOUND;
END;
$$;

CREATE FUNCTION public.renew_monitor_run_lease(
  p_user_id UUID, p_owner_id UUID, p_lease_seconds INTEGER DEFAULT 180
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_user_id IS NULL OR p_owner_id IS NULL
     OR p_lease_seconds < 30 OR p_lease_seconds > 900 THEN
    RAISE EXCEPTION 'Invalid monitor-run lease';
  END IF;

  UPDATE public.monitor_run_leases SET
    leased_until = clock_timestamp() + make_interval(secs => p_lease_seconds),
    updated_at = clock_timestamp()
  WHERE user_id = p_user_id AND owner_id = p_owner_id
    AND leased_until >= clock_timestamp();

  RETURN FOUND;
END;
$$;

CREATE FUNCTION public.release_monitor_run_lease(p_user_id UUID, p_owner_id UUID)
RETURNS VOID
LANGUAGE sql SECURITY INVOKER SET search_path = public
AS $$
  DELETE FROM public.monitor_run_leases
  WHERE user_id = p_user_id AND owner_id = p_owner_id;
$$;

REVOKE ALL ON FUNCTION public.claim_monitor_run_lease(UUID, UUID, INTEGER)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.renew_monitor_run_lease(UUID, UUID, INTEGER)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.release_monitor_run_lease(UUID, UUID)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_monitor_run_lease(UUID, UUID, INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.renew_monitor_run_lease(UUID, UUID, INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.release_monitor_run_lease(UUID, UUID) TO service_role;

COMMENT ON TABLE public.monitor_run_leases IS
  'Renewable per-user ownership for manual and scheduled monitor batches. This does not cover future paper-position ownership.';
