-- Fresh-database definition for the hypothetical daily trend portfolio track (#239 P14).
-- Rows and state are committed together under an account lock and an expected-prior-state check.
CREATE TABLE public.forward_trend_weights (
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  track TEXT NOT NULL CHECK (length(track) BETWEEN 1 AND 64),
  day_ms BIGINT NOT NULL CHECK (day_ms >= 0 AND day_ms % 86400000 = 0),
  decided_from_close_ms BIGINT NOT NULL CHECK (decided_from_close_ms = day_ms - 86400000),
  decided_at_ms BIGINT NOT NULL CHECK (decided_at_ms >= day_ms),
  recorded_at_ms BIGINT NOT NULL CHECK (recorded_at_ms >= decided_at_ms),
  sample_kind TEXT NOT NULL CHECK (sample_kind IN ('prospective', 'retrospective')),
  CHECK ((sample_kind = 'prospective') = (recorded_at_ms < day_ms + 86400000)),
  weights JSONB NOT NULL CHECK (jsonb_typeof(weights) = 'object'),
  defined JSONB NOT NULL CHECK (jsonb_typeof(defined) = 'object'),
  version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
  params_hash TEXT NOT NULL CHECK (params_hash ~ '^[0-9a-f]{64}$'),
  run_key TEXT NOT NULL CHECK (length(run_key) BETWEEN 1 AND 128),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id, track, day_ms)
);

CREATE TABLE public.forward_trend_ledger (
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  track TEXT NOT NULL CHECK (length(track) BETWEEN 1 AND 64),
  day_ms BIGINT NOT NULL CHECK (day_ms >= 0 AND day_ms % 86400000 = 0),
  daily_ppm BIGINT NOT NULL,
  equity_ppm BIGINT NOT NULL CHECK (equity_ppm >= 0),
  sample_kind TEXT NOT NULL CHECK (sample_kind IN ('prospective', 'retrospective')),
  sample_equity_ppm BIGINT NOT NULL CHECK (sample_equity_ppm >= 0),
  turnover_ppm BIGINT NOT NULL CHECK (turnover_ppm >= 0),
  gross_ppm BIGINT NOT NULL CHECK (gross_ppm >= 0),
  symbols_active INTEGER NOT NULL CHECK (symbols_active >= 0),
  weights JSONB NOT NULL CHECK (jsonb_typeof(weights) = 'object'),
  version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
  params_hash TEXT NOT NULL CHECK (params_hash ~ '^[0-9a-f]{64}$'),
  run_key TEXT NOT NULL CHECK (length(run_key) BETWEEN 1 AND 128),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id, track, day_ms),
  FOREIGN KEY (user_id, track, day_ms) REFERENCES public.forward_trend_weights(user_id, track, day_ms)
);
CREATE INDEX forward_trend_ledger_days ON public.forward_trend_ledger(user_id, day_ms, track);

-- Diagnostics have committed=false and can never become a resume checkpoint.
CREATE TABLE public.forward_trend_state (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  revision BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  committed BOOLEAN NOT NULL DEFAULT false,
  run_key TEXT NOT NULL CHECK (length(run_key) BETWEEN 1 AND 128),
  trigger TEXT NOT NULL CHECK (trigger IN ('daily', 'on_demand')),
  status TEXT NOT NULL CHECK (status IN ('ok', 'partial')),
  reason TEXT CHECK (reason IS NULL OR length(reason) <= 2000),
  last_complete_day_ms BIGINT NOT NULL CHECK (last_complete_day_ms % 86400000 = 0),
  through_day_ms BIGINT CHECK (through_day_ms IS NULL OR through_day_ms % 86400000 = 0),
  history_start_ms BIGINT NOT NULL CHECK (history_start_ms % 86400000 = 0),
  track_start_ms BIGINT NOT NULL CHECK (track_start_ms % 86400000 = 0),
  symbols JSONB NOT NULL CHECK (jsonb_typeof(symbols) = 'array' AND jsonb_array_length(symbols) > 0),
  states JSONB NOT NULL CHECK (jsonb_typeof(states) = 'object'),
  versions JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(versions) = 'object'),
  params_hash TEXT CHECK (params_hash ~ '^[0-9a-f]{64}$'),
  CHECK (NOT committed OR (params_hash IS NOT NULL AND states <> '{}'::jsonb)),
  tracks JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(tracks) = 'array'),
  funding_unavailable JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(funding_unavailable) = 'array'),
  assumptions JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(assumptions) = 'array'),
  freshness JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(freshness) = 'object'),
  counts JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(counts) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX forward_trend_committed_run ON public.forward_trend_state(user_id, run_key) WHERE committed;
CREATE INDEX forward_trend_state_latest ON public.forward_trend_state(user_id, revision DESC);

DO $$
DECLARE name TEXT;
BEGIN
  FOREACH name IN ARRAY ARRAY['forward_trend_weights', 'forward_trend_ledger', 'forward_trend_state'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON public.%I
      FOR EACH ROW EXECUTE FUNCTION public.reject_forward_mutation()', name || '_append_only', name);
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', name);
    EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC, anon, authenticated', name);
    EXECUTE format('GRANT SELECT ON public.%I TO authenticated', name);
    EXECUTE format('GRANT SELECT, INSERT ON public.%I TO service_role', name);
    EXECUTE format('CREATE POLICY %I ON public.%I FOR SELECT TO authenticated USING (auth.uid() = user_id)',
      'own ' || name, name);
  END LOOP;
END;
$$;

CREATE FUNCTION public.commit_forward_trend_run(
  p_user_id UUID, p_expected_state_id UUID, p_response JSONB, p_run JSONB
) RETURNS JSONB LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  prior public.forward_trend_state;
  item JSONB;
  existing JSONB;
  decision public.forward_trend_weights;
  recorded_ms BIGINT;
  weight_count INTEGER := 0;
  ledger_count INTEGER := 0;
  changed INTEGER;
  counts JSONB;
  track_state JSONB;
  track_name TEXT;
  kind TEXT;
BEGIN
  -- Serializes both initialization (no row to lock yet) and subsequent overlapping runs.
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-trend:' || p_user_id::text, 0));
  SELECT * INTO prior FROM public.forward_trend_state
    WHERE user_id = p_user_id AND committed ORDER BY revision DESC LIMIT 1;
  IF prior.id IS DISTINCT FROM p_expected_state_id THEN
    RAISE EXCEPTION 'Forward trend state changed; reload and retry' USING ERRCODE = '40001';
  END IF;
  IF prior.id IS NOT NULL AND (prior.params_hash IS DISTINCT FROM p_response->>'params_hash'
      OR prior.symbols IS DISTINCT FROM p_response->'symbols'
      OR prior.history_start_ms IS DISTINCT FROM (p_response->>'history_start_ms')::BIGINT
      OR prior.track_start_ms IS DISTINCT FROM (p_response->>'track_start_ms')::BIGINT) THEN
    RAISE EXCEPTION 'Forward trend configuration identity differs';
  END IF;
  IF p_response->'states' IS NULL OR p_response->'states' = '{}'::jsonb THEN
    RAISE EXCEPTION 'Cannot commit incomplete startup';
  END IF;
  IF (SELECT jsonb_agg(key ORDER BY key) FROM jsonb_each(p_response->'states')) IS DISTINCT FROM
      (SELECT jsonb_agg(value->>'name' ORDER BY value->>'name') FROM jsonb_array_elements(p_response->'tracks')) THEN
    RAISE EXCEPTION 'Cannot commit an incomplete set of tracks';
  END IF;
  recorded_ms := floor(extract(epoch FROM clock_timestamp()) * 1000)::BIGINT;

  -- Duplicate payloads must agree. Recording time and run key belong to the first insert.
  FOR item IN SELECT value FROM jsonb_array_elements(p_response->'weights') LOOP
    IF (SELECT jsonb_agg(key ORDER BY key) FROM jsonb_each(item->'weights')) IS DISTINCT FROM
        (SELECT jsonb_agg(value ORDER BY value) FROM jsonb_array_elements(p_response->'symbols')) OR
      (SELECT jsonb_agg(key ORDER BY key) FROM jsonb_each(item->'defined')) IS DISTINCT FROM
        (SELECT jsonb_agg(value ORDER BY value) FROM jsonb_array_elements(p_response->'symbols')) THEN
      RAISE EXCEPTION 'Cannot commit a decision without the complete frozen universe';
    END IF;
    SELECT to_jsonb(w) INTO existing FROM public.forward_trend_weights w
      WHERE user_id = p_user_id AND track = item->>'track' AND day_ms = (item->>'day_ms')::BIGINT;
    IF existing IS NOT NULL THEN
      IF (existing - ARRAY['user_id','run_key','created_at','recorded_at_ms','sample_kind']) IS DISTINCT FROM
          (item || jsonb_build_object('version',p_response->'versions'->>'trend_track','params_hash',p_response->>'params_hash')) THEN
        RAISE EXCEPTION 'Conflicting immutable trend decision';
      END IF;
    ELSE
      INSERT INTO public.forward_trend_weights
        (user_id,track,day_ms,decided_from_close_ms,decided_at_ms,recorded_at_ms,sample_kind,
         weights,defined,version,params_hash,run_key)
      VALUES (p_user_id,item->>'track',(item->>'day_ms')::BIGINT,(item->>'decided_from_close_ms')::BIGINT,
        (item->>'decided_at_ms')::BIGINT,recorded_ms,
        CASE WHEN recorded_ms < (item->>'day_ms')::BIGINT + 86400000 THEN 'prospective' ELSE 'retrospective' END,
        item->'weights',item->'defined',p_response->'versions'->>'trend_track',p_response->>'params_hash',p_run->>'run_key');
      weight_count := weight_count + 1;
    END IF;
  END LOOP;
  FOR item IN SELECT value FROM jsonb_array_elements(p_response->'ledger') LOOP
    SELECT * INTO decision FROM public.forward_trend_weights
      WHERE user_id = p_user_id AND track = item->>'track' AND day_ms = (item->>'day_ms')::BIGINT;
    IF decision.day_ms IS NULL OR decision.weights IS DISTINCT FROM item->'weights'
        OR decision.sample_kind IS DISTINCT FROM item->>'sample_kind'
        OR decision.params_hash IS DISTINCT FROM p_response->>'params_hash' THEN
      RAISE EXCEPTION 'Ledger does not match recorded decision';
    END IF;
    IF (item->>'day_ms')::BIGINT + 86400000 > recorded_ms THEN
      RAISE EXCEPTION 'Cannot finalize an incomplete day';
    END IF;
    SELECT to_jsonb(l) INTO existing FROM public.forward_trend_ledger l
      WHERE user_id = p_user_id AND track = item->>'track' AND day_ms = (item->>'day_ms')::BIGINT;
    IF existing IS NOT NULL THEN
      IF (existing - ARRAY['user_id','run_key','created_at']) IS DISTINCT FROM
          (item || jsonb_build_object('version',p_response->'versions'->>'trend_track','params_hash',p_response->>'params_hash')) THEN
        RAISE EXCEPTION 'Conflicting immutable trend ledger';
      END IF;
    ELSE
      INSERT INTO public.forward_trend_ledger
        (user_id,track,day_ms,daily_ppm,equity_ppm,sample_kind,sample_equity_ppm,
         turnover_ppm,gross_ppm,symbols_active,weights,version,params_hash,run_key)
      VALUES (p_user_id,item->>'track',(item->>'day_ms')::BIGINT,(item->>'daily_ppm')::BIGINT,
        (item->>'equity_ppm')::BIGINT,item->>'sample_kind',(item->>'sample_equity_ppm')::BIGINT,
        (item->>'turnover_ppm')::BIGINT,(item->>'gross_ppm')::BIGINT,(item->>'symbols_active')::INTEGER,
        item->'weights',p_response->'versions'->>'trend_track',p_response->>'params_hash',p_run->>'run_key');
      ledger_count := ledger_count + 1;
    END IF;
  END LOOP;

  -- No checkpoint can claim ledger days or sample counts that were not committed.
  FOR track_name, track_state IN SELECT key,value FROM jsonb_each(p_response->'states') LOOP
    SELECT count(*) INTO changed FROM public.forward_trend_ledger WHERE user_id=p_user_id AND track=track_name;
    IF changed <> (track_state->>'n_days')::INTEGER OR
      (SELECT max(day_ms) FROM public.forward_trend_ledger WHERE user_id=p_user_id AND track=track_name)
        IS DISTINCT FROM (track_state->>'last_day_ms')::BIGINT THEN
      RAISE EXCEPTION 'Trend state does not match committed ledger';
    END IF;
    FOREACH kind IN ARRAY ARRAY['prospective','retrospective'] LOOP
      SELECT count(*) INTO changed FROM public.forward_trend_ledger
        WHERE user_id=p_user_id AND track=track_name AND sample_kind=kind;
      IF changed <> (track_state->'samples'->kind->>'n_days')::INTEGER THEN
        RAISE EXCEPTION 'Trend sample count does not match recorded decisions';
      END IF;
    END LOOP;
  END LOOP;
  counts := coalesce(p_run->'counts','{}'::jsonb) || jsonb_build_object('weights',weight_count,'ledger',ledger_count);
  INSERT INTO public.forward_trend_state
    (user_id,committed,run_key,trigger,status,reason,last_complete_day_ms,through_day_ms,
     history_start_ms,track_start_ms,symbols,states,versions,params_hash,tracks,
     funding_unavailable,assumptions,freshness,counts)
  VALUES (p_user_id,true,p_run->>'run_key',p_run->>'trigger',p_run->>'status',p_run->>'reason',
    (p_run->>'last_complete_day_ms')::BIGINT,(p_response->>'through_day_ms')::BIGINT,
    (p_response->>'history_start_ms')::BIGINT,(p_response->>'track_start_ms')::BIGINT,
    p_response->'symbols',p_response->'states',p_response->'versions',p_response->>'params_hash',p_response->'tracks',
    p_response->'funding_unavailable',p_response->'assumptions',coalesce(p_run->'freshness','{}'::jsonb),counts);
  RETURN counts;
END;
$$;
REVOKE ALL ON FUNCTION public.commit_forward_trend_run(UUID,UUID,JSONB,JSONB) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.commit_forward_trend_run(UUID,UUID,JSONB,JSONB) TO service_role;

COMMENT ON COLUMN public.forward_trend_weights.sample_kind IS
  'Prospective means actually recorded before the daily closing outcome; the hypothetical weight may be recorded after the open.';
