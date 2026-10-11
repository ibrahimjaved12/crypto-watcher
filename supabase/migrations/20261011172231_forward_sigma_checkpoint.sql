-- Daily, one-symbol checkpoint; hourly market commits no longer carry sigma payloads.
-- Legacy sigma RPCs stay available for rollback.
CREATE FUNCTION public.commit_forward_sigma_checkpoint(p_state JSONB, p_expected_checksum TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE prior public.forward_sigma_state;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-sigma:' || (p_state->>'symbol'), 0));
  SELECT * INTO prior FROM public.forward_sigma_state
    WHERE symbol=p_state->>'symbol' AND sigma_version=p_state->>'sigma_version';
  IF prior.checksum IS DISTINCT FROM p_expected_checksum THEN
    RAISE EXCEPTION 'Forward sigma checksum conflict; restore hosted state' USING ERRCODE='40001';
  END IF;
  IF prior.as_of_ms > (p_state->>'as_of_ms')::BIGINT THEN
    RAISE EXCEPTION 'Forward sigma checkpoint cannot move backwards';
  END IF;
  IF prior.symbol IS NOT NULL AND prior.checksum IS NOT DISTINCT FROM p_state->>'checksum' THEN RETURN; END IF;
  INSERT INTO public.forward_sigma_state(symbol,sigma_version,as_of_ms,payload,checksum)
    VALUES(p_state->>'symbol',p_state->>'sigma_version',(p_state->>'as_of_ms')::BIGINT,
           p_state->'payload',p_state->>'checksum')
  ON CONFLICT(symbol,sigma_version) DO UPDATE SET as_of_ms=excluded.as_of_ms,payload=excluded.payload,
    checksum=excluded.checksum,updated_at=clock_timestamp();
END;
$$;
REVOKE ALL ON FUNCTION public.commit_forward_sigma_checkpoint(JSONB,TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.commit_forward_sigma_checkpoint(JSONB,TEXT) TO service_role;
