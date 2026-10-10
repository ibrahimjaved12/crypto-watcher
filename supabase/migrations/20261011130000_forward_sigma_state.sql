-- Global market state contains no account/wallet data. Only the service role may access it.
CREATE TABLE public.forward_sigma_state (
  symbol TEXT NOT NULL CHECK (symbol ~ '^[A-Z0-9]{5,16}$'),
  sigma_version TEXT NOT NULL,
  as_of_ms BIGINT NOT NULL CHECK (as_of_ms >= 0),
  payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  checksum TEXT NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (symbol, sigma_version)
);
ALTER TABLE public.forward_sigma_state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.forward_sigma_state FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.forward_sigma_state TO service_role;

-- Called after Python and TypeScript have verified the seed's version and canonical checksum.
-- Initial seeding is idempotent, but cannot overwrite a running checkpoint.
CREATE FUNCTION public.seed_forward_sigma_state(p_state JSONB)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE prior public.forward_sigma_state;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-sigma:' || (p_state->>'symbol'), 0));
  SELECT * INTO prior FROM public.forward_sigma_state
    WHERE symbol=p_state->>'symbol' AND sigma_version=p_state->>'sigma_version';
  IF prior.symbol IS NOT NULL THEN
    IF prior.checksum IS DISTINCT FROM p_state->>'checksum' THEN
      RAISE EXCEPTION 'Sigma state already exists; refusing seed overwrite';
    END IF;
    RETURN;
  END IF;
  INSERT INTO public.forward_sigma_state(symbol,sigma_version,as_of_ms,payload,checksum)
  VALUES(p_state->>'symbol',p_state->>'sigma_version',(p_state->>'as_of_ms')::BIGINT,
         p_state->'payload',p_state->>'checksum');
END;
$$;

CREATE FUNCTION public.commit_forward_sigma_run(
  p_user_id UUID, p_run JSONB, p_records JSONB, p_states JSONB,
  p_expected JSONB, p_expected_run_id UUID
) RETURNS JSONB LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  run_id UUID := gen_random_uuid();
  latest_id UUID;
  item JSONB;
  symbol_name TEXT;
  prior public.forward_sigma_state;
  owner_fields JSONB;
  counts JSONB := '{}'::jsonb;
  table_name TEXT;
  group_name TEXT;
  changed INTEGER;
  total INTEGER;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-wallet:' || p_user_id::TEXT, 0));
  -- Same hour wins once, including concurrent submissions. Do not touch the checkpoint on replay.
  IF EXISTS(SELECT 1 FROM public.paper_runs WHERE user_id=p_user_id AND paper_account='default'
      AND run_key=p_run->>'run_key' AND status NOT IN ('skipped_stale','no_sigma')) THEN
    RETURN jsonb_build_object('already_done',1);
  END IF;
  SELECT id INTO latest_id FROM public.paper_runs WHERE user_id=p_user_id AND paper_account='default'
    AND status='ok' ORDER BY boundary_ms DESC LIMIT 1;
  IF latest_id IS DISTINCT FROM p_expected_run_id THEN
    RAISE EXCEPTION 'Forward wallet changed; reload and retry' USING ERRCODE='40001';
  END IF;
  IF p_run->>'status' <> 'ok' AND jsonb_array_length(p_states) <> 0 THEN
    RAISE EXCEPTION 'Retryable runs cannot advance sigma state';
  END IF;
  -- Lock global symbols in stable order to avoid multi-account deadlocks.
  FOR symbol_name IN SELECT value->>'symbol' FROM jsonb_array_elements(p_states) ORDER BY 1 LOOP
    PERFORM pg_advisory_xact_lock(hashtextextended('forward-sigma:' || symbol_name, 0));
  END LOOP;
  FOR item IN SELECT value FROM jsonb_array_elements(p_states) LOOP
    SELECT * INTO prior FROM public.forward_sigma_state
      WHERE symbol=item->>'symbol' AND sigma_version=item->>'sigma_version';
    IF prior.checksum IS DISTINCT FROM p_expected->>(item->>'symbol') THEN
      -- Another account may already have committed precisely the same market result.
      IF prior.checksum IS DISTINCT FROM item->>'checksum' THEN
        RAISE EXCEPTION 'Forward sigma changed; reload and retry' USING ERRCODE='40001';
      END IF;
    END IF;
    IF (item->>'as_of_ms')::BIGINT <> (p_run->>'boundary_ms')::BIGINT - 60000 THEN
      RAISE EXCEPTION 'Sigma state does not match run boundary';
    END IF;
  END LOOP;
  INSERT INTO public.paper_runs SELECT (jsonb_populate_record(NULL::public.paper_runs,
    jsonb_build_object('processed_to_ms','{}'::jsonb,'versions','{}'::jsonb,'assumptions','[]'::jsonb,
      'reasons','{}'::jsonb,'counts','{}'::jsonb,'freshness','{}'::jsonb) || p_run ||
    jsonb_build_object('id',run_id,'user_id',p_user_id,'paper_account','default','created_at',clock_timestamp()))).*;
  owner_fields := jsonb_build_object('user_id',p_user_id,'run_id',run_id,'created_at',clock_timestamp());
  FOREACH group_name IN ARRAY ARRAY['signals','setups','outcomes','ledger'] LOOP
    table_name := CASE group_name WHEN 'ledger' THEN 'paper_ledger' ELSE 'forward_' || group_name END;
    total := 0;
    FOR item IN SELECT value FROM jsonb_array_elements(p_records->group_name) LOOP
      -- Schema names are fixed above; records have already been validated at the service boundary.
      EXECUTE format('INSERT INTO public.%I SELECT (jsonb_populate_record(NULL::public.%I, $1)).* ON CONFLICT DO NOTHING',
                     table_name,table_name) USING item || owner_fields;
      GET DIAGNOSTICS changed = ROW_COUNT;
      total := total + changed;
    END LOOP;
    counts := counts || jsonb_build_object(group_name,total);
  END LOOP;
  FOR item IN SELECT value FROM jsonb_array_elements(p_states) LOOP
    INSERT INTO public.forward_sigma_state(symbol,sigma_version,as_of_ms,payload,checksum)
    VALUES(item->>'symbol',item->>'sigma_version',(item->>'as_of_ms')::BIGINT,item->'payload',item->>'checksum')
    ON CONFLICT(symbol,sigma_version) DO UPDATE SET as_of_ms=excluded.as_of_ms,payload=excluded.payload,
      checksum=excluded.checksum,updated_at=clock_timestamp()
    WHERE forward_sigma_state.as_of_ms <= excluded.as_of_ms AND forward_sigma_state.checksum <> excluded.checksum;
  END LOOP;
  RETURN counts;
END;
$$;
REVOKE ALL ON FUNCTION public.seed_forward_sigma_state(JSONB),
  public.commit_forward_sigma_run(UUID,JSONB,JSONB,JSONB,JSONB,UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.seed_forward_sigma_state(JSONB),
  public.commit_forward_sigma_run(UUID,JSONB,JSONB,JSONB,JSONB,UUID) TO service_role;
