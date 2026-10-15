-- Published copy of the movement engine's persisted current state (#30), so the deployed site (which cannot
-- read the machine's operational database) can show the market strip. Market-wide, not per account: ONE row per
-- universe, overwritten in place, no history. The machine publishes through the site hook (shared machine
-- secret); only the service role writes. Authenticated users read the row.
CREATE TABLE public.market_state_published (
  universe_id TEXT PRIMARY KEY CHECK (length(universe_id) BETWEEN 1 AND 128),
  evaluation_boundary_ms BIGINT NOT NULL CHECK (evaluation_boundary_ms > 0),
  payload JSONB NOT NULL,
  payload_bytes INTEGER NOT NULL CHECK (payload_bytes BETWEEN 1 AND 524288),
  version TEXT NOT NULL DEFAULT 'market-state-published-v1',
  published_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.market_state_published IS
  'Latest persisted movement-engine current state, copied from the machine (one row per universe, bounded to 512 KiB, overwritten in place, no history).';
COMMENT ON COLUMN public.market_state_published.payload IS
  'The same payload getMarketMovementCurrentState maps from (PersistedMarketStateCurrent without the lifecycle state): direction, pace, versions and the bounded current evidence. Market data only; no account data.';
COMMENT ON COLUMN public.market_state_published.evaluation_boundary_ms IS
  'The engine''s own evaluation boundary time of the payload (ms). The engine STALE rule applies to this, not to published_at.';
COMMENT ON COLUMN public.market_state_published.published_at IS
  'Database time of the last publish OR heartbeat from the machine. Older than the machine-offline window means the machine stopped reporting.';

ALTER TABLE public.market_state_published ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.market_state_published FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.market_state_published TO authenticated;
GRANT ALL ON public.market_state_published TO service_role;
CREATE POLICY market_state_published_read ON public.market_state_published FOR SELECT TO authenticated
  USING (true);
-- No INSERT/UPDATE/DELETE policy for users.

-- Overwrite the row with a newer (or equal) evaluation; an older boundary never replaces a newer one.
CREATE FUNCTION public.publish_market_state(p_universe_id TEXT, p_boundary_ms BIGINT, p_payload JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
DECLARE
  size INTEGER := octet_length(p_payload::TEXT);
BEGIN
  IF p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object' THEN
    RAISE EXCEPTION 'Market state payload must be an object';
  END IF;
  IF size > 524288 THEN
    RAISE EXCEPTION 'Market state payload too large';
  END IF;
  INSERT INTO public.market_state_published AS m (universe_id, evaluation_boundary_ms, payload, payload_bytes, published_at)
    VALUES (p_universe_id, p_boundary_ms, p_payload, size, now())
  ON CONFLICT (universe_id) DO UPDATE SET evaluation_boundary_ms = excluded.evaluation_boundary_ms,
      payload = excluded.payload, payload_bytes = excluded.payload_bytes, published_at = now()
    WHERE excluded.evaluation_boundary_ms >= m.evaluation_boundary_ms;
  RETURN FOUND;
END;
$$;

-- The machine is alive and the state is unchanged: advance published_at only.
CREATE FUNCTION public.touch_market_state(p_universe_id TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  UPDATE public.market_state_published SET published_at = now() WHERE universe_id = p_universe_id;
  RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION public.publish_market_state(TEXT, BIGINT, JSONB) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.touch_market_state(TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.publish_market_state(TEXT, BIGINT, JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.touch_market_state(TEXT) TO service_role;
