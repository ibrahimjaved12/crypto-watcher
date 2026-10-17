-- #374 follow-ups to 20261018100000.
-- Trap: jsonb_populate_record never applies column defaults (a key missing from the JSON becomes NULL), so every
-- NOT NULL DEFAULT column must be supplied explicitly in the JSON; here fx sessions/detail get their defaults.
CREATE OR REPLACE FUNCTION public.commit_fx_stage(p_stage JSONB,p_records JSONB)
RETURNS UUID LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE existing UUID; item JSONB;
  stage_id UUID := (p_stage->>'id')::UUID; boundary BIGINT := (p_stage->>'boundary_ms')::BIGINT;
  grp TEXT; tbl TEXT;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-market-fx',0));
  SELECT id INTO existing FROM public.fx_market_stages WHERE boundary_ms=boundary;
  IF existing IS NOT NULL THEN RETURN existing; END IF;
  IF EXISTS(SELECT 1 FROM public.fx_market_stages WHERE boundary_ms>boundary) THEN
    RAISE EXCEPTION 'FX stage is older than committed cursor' USING ERRCODE='40001';
  END IF;
  INSERT INTO public.fx_market_stages(id,boundary_ms,versions,params_hash,payload,freshness)
    VALUES(stage_id,boundary,p_stage->'versions',p_stage->>'params_hash',p_stage->'payload',coalesce(p_stage->'freshness','{}'));
  FOREACH grp IN ARRAY ARRAY['signals','suppressed','setups','outcomes'] LOOP
    tbl := 'fx_market_'||grp;
    FOR item IN SELECT value FROM jsonb_array_elements(coalesce(p_records->grp,'[]')) LOOP
      EXECUTE format('INSERT INTO public.%I SELECT (jsonb_populate_record(NULL::public.%I,$1)).* ON CONFLICT DO NOTHING',tbl,tbl)
        -- Defaults first so a supplied value wins; keys a table lacks are ignored by jsonb_populate_record.
        USING '{"sessions":[],"detail":{}}'::jsonb || item || jsonb_build_object('stage_run_id',stage_id,'created_at',clock_timestamp());
    END LOOP;
  END LOOP;
  FOR item IN SELECT value FROM jsonb_array_elements(coalesce(p_records->'events','[]')) LOOP
    IF item->>'stage_run_id' IS DISTINCT FROM stage_id::TEXT THEN RAISE EXCEPTION 'Event stage mismatch'; END IF;
    INSERT INTO public.fx_market_events VALUES(stage_id,item->>'symbol',(item->>'sequence')::INTEGER,
      (item->>'ms')::BIGINT,item->>'type',item);
  END LOOP;
  RETURN stage_id;
END $$;
REVOKE ALL ON FUNCTION public.commit_fx_stage(JSONB,JSONB) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.commit_fx_stage(JSONB,JSONB) TO service_role;

-- RLS: the sub-select makes has_market() an initplan evaluated once per statement, not once per row.
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['fx_market_stages','fx_market_signals','fx_market_suppressed','fx_market_setups',
    'fx_market_outcomes','fx_market_events'] LOOP
    EXECUTE format('DROP POLICY fx_entitled ON public.%I',t);
    EXECUTE format('CREATE POLICY fx_entitled ON public.%I FOR SELECT TO authenticated USING ((SELECT public.has_market(''forex'')))',t);
  END LOOP;
END $$;
