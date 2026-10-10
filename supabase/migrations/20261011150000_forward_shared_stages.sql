-- P29 additive cutover. Legacy account-scoped market tables and RPC remain for rollback.
CREATE TABLE public.forward_market_stages (
  id UUID PRIMARY KEY,
  boundary_ms BIGINT NOT NULL UNIQUE CHECK (boundary_ms >= 0),
  versions JSONB NOT NULL,
  params_hash TEXT,
  payload JSONB NOT NULL,
  freshness JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.forward_market_events (
  stage_run_id UUID NOT NULL REFERENCES public.forward_market_stages,
  symbol TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence >= 0),
  ms BIGINT NOT NULL CHECK (ms >= 0),
  type TEXT NOT NULL CHECK (type IN ('open','close','funding')),
  payload JSONB NOT NULL,
  PRIMARY KEY(stage_run_id,symbol,sequence)
);
CREATE TABLE public.forward_market_paths (
  stage_run_id UUID NOT NULL REFERENCES public.forward_market_stages,
  setup_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  PRIMARY KEY(stage_run_id,setup_id)
);
-- Copy column/check definitions only. All changes below apply to NEW tables, not legacy rows.
CREATE TABLE public.forward_market_signals (LIKE public.forward_signals INCLUDING DEFAULTS INCLUDING CONSTRAINTS);
CREATE TABLE public.forward_market_setups (LIKE public.forward_setups INCLUDING DEFAULTS INCLUDING CONSTRAINTS);
CREATE TABLE public.forward_market_outcomes (LIKE public.forward_outcomes INCLUDING DEFAULTS INCLUDING CONSTRAINTS);
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['signals','setups','outcomes'] LOOP
    EXECUTE format('ALTER TABLE public.%I DROP COLUMN user_id, DROP COLUMN run_id','forward_market_'||t);
    EXECUTE format('ALTER TABLE public.%I ADD COLUMN stage_run_id UUID REFERENCES public.forward_market_stages','forward_market_'||t);
  END LOOP;
END $$;
ALTER TABLE public.forward_market_signals ADD PRIMARY KEY(signal_id);
ALTER TABLE public.forward_market_setups ADD PRIMARY KEY(setup_id);
ALTER TABLE public.forward_market_setups ADD FOREIGN KEY(signal_id) REFERENCES public.forward_market_signals;
ALTER TABLE public.forward_market_outcomes ADD PRIMARY KEY(setup_id,status);
ALTER TABLE public.forward_market_outcomes ADD FOREIGN KEY(setup_id) REFERENCES public.forward_market_setups;
CREATE INDEX forward_market_setups_entry ON public.forward_market_setups(entry_ms DESC);
CREATE INDEX forward_market_signals_time ON public.forward_market_signals(signal_ms DESC);
CREATE INDEX forward_market_events_time ON public.forward_market_events(stage_run_id,ms,symbol,sequence);

INSERT INTO public.forward_market_stages(id,boundary_ms,versions,params_hash,payload,freshness,created_at)
SELECT DISTINCT ON (boundary_ms) id,boundary_ms,versions,params_hash,
  jsonb_build_object('schema_version',1,'versions',versions,'params_hash',params_hash,
    'processed_to_ms',processed_to_ms,'funding_unavailable','[]'::jsonb,'signals','[]'::jsonb,
    'setups','[]'::jsonb,'resolutions','[]'::jsonb,'reasons',reasons,'assumptions','[]'::jsonb),freshness,created_at
FROM public.paper_runs WHERE status='ok' ORDER BY boundary_ms,created_at,id;
-- Deterministic de-duplication of identical market facts across the old user copies.
DO $$
DECLARE t TEXT; cols TEXT; projected TEXT; identity_cols TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['signals','setups','outcomes'] LOOP
    SELECT string_agg(quote_ident(attname),',' ORDER BY attnum),
           string_agg('old.' || quote_ident(attname),',' ORDER BY attnum)
      INTO cols,projected FROM pg_attribute
      WHERE attrelid=('public.forward_'||t)::regclass AND attnum>0 AND NOT attisdropped
        AND attname NOT IN ('user_id','run_id');
    identity_cols := CASE t WHEN 'signals' THEN 'old.signal_id' WHEN 'setups' THEN 'old.setup_id'
      ELSE 'old.setup_id,old.status' END;
    EXECUTE format('INSERT INTO public.%I (%s,stage_run_id)
      SELECT DISTINCT ON (%s) %s,stage.id FROM public.%I old
      JOIN public.paper_runs run ON run.id=old.run_id
      JOIN public.forward_market_stages stage ON stage.boundary_ms=run.boundary_ms
      ORDER BY %s,old.created_at,old.user_id','forward_market_'||t,cols,identity_cols,projected,'forward_'||t,identity_cols);
  END LOOP;
END $$;

-- Each auth user is an account. Legacy paper_account remains untouched; migration records
-- its mapping so the owner's balances/ledger/sequence continue on their existing account.
CREATE TABLE public.forward_wallet_accounts (
  user_id UUID PRIMARY KEY REFERENCES auth.users ON DELETE CASCADE,
  account_id TEXT NOT NULL,
  config JSONB NOT NULL DEFAULT '{}',
  start_boundary_ms BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO public.forward_wallet_accounts(user_id,account_id,config,start_boundary_ms)
SELECT DISTINCT ON (user_id) user_id,paper_account,coalesce(wallet_config,'{}'),boundary_ms
FROM public.paper_runs WHERE status='ok' ORDER BY user_id,boundary_ms DESC,created_at DESC,id;
ALTER TABLE public.paper_runs ADD COLUMN stage_run_id UUID REFERENCES public.forward_market_stages;
ALTER TABLE public.paper_runs ADD COLUMN consumed_boundary_ms BIGINT;
CREATE UNIQUE INDEX paper_runs_shared_hour ON public.paper_runs(user_id,boundary_ms)
  WHERE stage_run_id IS NOT NULL AND status='ok';

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['forward_market_stages','forward_market_events','forward_market_paths',
    'forward_market_signals','forward_market_setups','forward_market_outcomes','forward_wallet_accounts'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC,anon,authenticated',t);
    EXECUTE format('GRANT SELECT ON public.%I TO authenticated',t);
    EXECUTE format('GRANT SELECT,INSERT ON public.%I TO service_role',t);
    IF t='forward_wallet_accounts' THEN
      EXECUTE format('CREATE POLICY own_account ON public.%I FOR SELECT TO authenticated USING (auth.uid()=user_id)',t);
      EXECUTE format('GRANT UPDATE(config) ON public.%I TO service_role',t);
    ELSE
      EXECUTE format('CREATE POLICY shared_market ON public.%I FOR SELECT TO authenticated USING (true)',t);
      EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.reject_forward_mutation()',t||'_append_only',t);
    END IF;
  END LOOP;
END $$;

CREATE FUNCTION public.ensure_forward_account(p_user_id UUID,p_boundary_ms BIGINT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-wallet:'||p_user_id::TEXT,0));
  INSERT INTO public.forward_wallet_accounts(user_id,account_id,start_boundary_ms)
    VALUES(p_user_id,p_user_id::TEXT,p_boundary_ms) ON CONFLICT DO NOTHING;
END $$;

CREATE FUNCTION public.commit_market_stage(p_stage JSONB,p_records JSONB,p_states JSONB,p_expected JSONB)
RETURNS UUID LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE existing UUID; item JSONB; sym TEXT; prior public.forward_sigma_state;
  stage_id UUID := (p_stage->>'id')::UUID; boundary BIGINT := (p_stage->>'boundary_ms')::BIGINT;
  grp TEXT; tbl TEXT;
BEGIN
  -- Serialize market commits, then checkpoint locks in symbol order (also used by seeding).
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-market',0));
  SELECT id INTO existing FROM public.forward_market_stages WHERE boundary_ms=boundary;
  IF existing IS NOT NULL THEN RETURN existing; END IF;
  IF EXISTS(SELECT 1 FROM public.forward_market_stages WHERE boundary_ms>boundary) THEN
    RAISE EXCEPTION 'Market stage is older than committed cursor' USING ERRCODE='40001';
  END IF;
  FOR sym IN SELECT value->>'symbol' FROM jsonb_array_elements(p_states) ORDER BY 1 LOOP
    PERFORM pg_advisory_xact_lock(hashtextextended('forward-sigma:'||sym,0));
  END LOOP;
  FOR item IN SELECT value FROM jsonb_array_elements(p_states) LOOP
    SELECT * INTO prior FROM public.forward_sigma_state
      WHERE symbol=item->>'symbol' AND sigma_version=item->>'sigma_version';
    IF prior.checksum IS DISTINCT FROM p_expected->>(item->>'symbol') THEN
      RAISE EXCEPTION 'Sigma changed; reload market stage' USING ERRCODE='40001';
    END IF;
    IF (item->>'as_of_ms')::BIGINT <> boundary-60000 OR prior.as_of_ms > (item->>'as_of_ms')::BIGINT THEN
      RAISE EXCEPTION 'Invalid sigma stage boundary';
    END IF;
  END LOOP;
  INSERT INTO public.forward_market_stages(id,boundary_ms,versions,params_hash,payload,freshness)
    VALUES(stage_id,boundary,p_stage->'versions',p_stage->>'params_hash',p_stage->'payload',coalesce(p_stage->'freshness','{}'));
  FOREACH grp IN ARRAY ARRAY['signals','setups','outcomes'] LOOP
    tbl := 'forward_market_'||grp;
    FOR item IN SELECT value FROM jsonb_array_elements(p_records->grp) LOOP
      EXECUTE format('INSERT INTO public.%I SELECT (jsonb_populate_record(NULL::public.%I,$1)).* ON CONFLICT DO NOTHING',tbl,tbl)
        USING item || jsonb_build_object('stage_run_id',stage_id,'created_at',clock_timestamp());
    END LOOP;
  END LOOP;
  FOR item IN SELECT value FROM jsonb_array_elements(p_records->'events') LOOP
    IF item->>'stage_run_id' IS DISTINCT FROM stage_id::TEXT THEN RAISE EXCEPTION 'Event stage mismatch'; END IF;
    INSERT INTO public.forward_market_events VALUES(stage_id,item->>'symbol',(item->>'sequence')::INTEGER,
      (item->>'ms')::BIGINT,item->>'type',item);
  END LOOP;
  FOR item IN SELECT value FROM jsonb_array_elements(p_records->'adverse_paths') LOOP
    INSERT INTO public.forward_market_paths VALUES(stage_id,item->>'setup_id',item);
  END LOOP;
  FOR item IN SELECT value FROM jsonb_array_elements(p_states) LOOP
    INSERT INTO public.forward_sigma_state(symbol,sigma_version,as_of_ms,payload,checksum)
      VALUES(item->>'symbol',item->>'sigma_version',(item->>'as_of_ms')::BIGINT,item->'payload',item->>'checksum')
    ON CONFLICT(symbol,sigma_version) DO UPDATE SET as_of_ms=excluded.as_of_ms,payload=excluded.payload,
      checksum=excluded.checksum,updated_at=clock_timestamp()
      WHERE forward_sigma_state.checksum<>excluded.checksum;
  END LOOP;
  RETURN stage_id;
END $$;

CREATE FUNCTION public.commit_user_run(p_user_id UUID,p_stage_run_id UUID,p_run JSONB,p_ledger JSONB,p_expected_run_id UUID)
RETURNS UUID LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE account public.forward_wallet_accounts; stage public.forward_market_stages;
  latest public.paper_runs; existing UUID; run_id UUID := gen_random_uuid(); cursor_ms BIGINT; item JSONB;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-wallet:'||p_user_id::TEXT,0));
  SELECT * INTO account FROM public.forward_wallet_accounts WHERE user_id=p_user_id;
  IF account.user_id IS NULL THEN RAISE EXCEPTION 'Account not initialized'; END IF;
  SELECT * INTO stage FROM public.forward_market_stages WHERE id=p_stage_run_id;
  IF stage.id IS NULL THEN RAISE EXCEPTION 'Market stage missing'; END IF;
  SELECT id INTO existing FROM public.paper_runs WHERE user_id=p_user_id AND stage_run_id=stage.id AND status='ok';
  IF existing IS NOT NULL THEN RETURN existing; END IF;
  SELECT * INTO latest FROM public.paper_runs WHERE user_id=p_user_id AND paper_account=account.account_id AND status='ok'
    ORDER BY boundary_ms DESC,created_at DESC,id DESC LIMIT 1;
  IF latest.id IS DISTINCT FROM p_expected_run_id THEN
    RAISE EXCEPTION 'Wallet changed; reload and retry' USING ERRCODE='40001';
  END IF;
  cursor_ms := greatest(account.start_boundary_ms,coalesce(latest.consumed_boundary_ms,latest.boundary_ms,0));
  IF stage.boundary_ms<=cursor_ms OR EXISTS(SELECT 1 FROM public.forward_market_stages
      WHERE boundary_ms>cursor_ms AND boundary_ms<stage.boundary_ms) THEN
    RAISE EXCEPTION 'Wallet stages must be consumed in order';
  END IF;
  IF p_run->>'status' NOT IN ('ok','skipped_stale','no_sigma') THEN RAISE EXCEPTION 'Invalid user run status'; END IF;
  IF p_run->>'status'<>'ok' AND jsonb_array_length(p_ledger)>0 THEN RAISE EXCEPTION 'Retryable ledger must be empty'; END IF;
  INSERT INTO public.paper_runs SELECT (jsonb_populate_record(NULL::public.paper_runs,
    jsonb_build_object('processed_to_ms','{}'::jsonb,'versions','{}'::jsonb,'assumptions','[]'::jsonb,
      'reasons','{}'::jsonb,'counts','{}'::jsonb,'freshness','{}'::jsonb) || p_run ||
    jsonb_build_object('id',run_id,'user_id',p_user_id,'paper_account',account.account_id,
      'stage_run_id',stage.id,'boundary_ms',stage.boundary_ms,'run_key','hour:'||stage.boundary_ms,
      'consumed_boundary_ms',CASE WHEN p_run->>'status'='ok' THEN stage.boundary_ms ELSE NULL END,
      'created_at',clock_timestamp()))).*;
  FOR item IN SELECT value FROM jsonb_array_elements(p_ledger) LOOP
    INSERT INTO public.paper_ledger(user_id,paper_account,seq,ms,type,setup_id,symbol,amount_e8,balance_e8,payload,run_id)
      VALUES(p_user_id,account.account_id,(item->>'seq')::BIGINT,(item->>'ms')::BIGINT,item->>'type',
        item->>'setup_id',item->>'symbol',(item->>'amount_e8')::BIGINT,(item->>'balance_e8')::BIGINT,item,run_id);
  END LOOP;
  RETURN run_id;
END $$;
REVOKE ALL ON FUNCTION public.ensure_forward_account(UUID,BIGINT),public.commit_market_stage(JSONB,JSONB,JSONB,JSONB),
  public.commit_user_run(UUID,UUID,JSONB,JSONB,UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.ensure_forward_account(UUID,BIGINT),public.commit_market_stage(JSONB,JSONB,JSONB,JSONB),
  public.commit_user_run(UUID,UUID,JSONB,JSONB,UUID) TO service_role;

CREATE VIEW public.forward_market_open_setups WITH (security_invoker=true) AS
SELECT s.* FROM public.forward_market_setups s WHERE s.status='T' AND NOT EXISTS
  (SELECT 1 FROM public.forward_market_outcomes o WHERE o.setup_id=s.setup_id AND o.final);
CREATE VIEW public.forward_market_outcome_log WITH (security_invoker=true) AS
SELECT s.setup_id,s.strategy_id,s.version,s.symbol,s.side,s.horizon_min,s.rr,s.entry_ms,
  o.status,o.net_ur,o.cost_ur,o.fund_ur,o.exit_ms
FROM public.forward_market_setups s JOIN public.forward_market_outcomes o ON o.setup_id=s.setup_id AND o.final;
REVOKE ALL ON public.forward_market_open_setups,public.forward_market_outcome_log FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.forward_market_open_setups,public.forward_market_outcome_log TO authenticated,service_role;

-- Existing public report contracts now read the shared facts. Legacy tables/views remain.
CREATE OR REPLACE FUNCTION public.forward_outcome_report(
  p_from_ms BIGINT, p_to_ms BIGINT, p_strategy_ids TEXT[], p_symbols TEXT[],
  p_horizons INTEGER[], p_sides INTEGER[], p_rr TEXT[]
)
RETURNS TABLE (
  strategy_id TEXT, version TEXT, horizon_min INTEGER, rr TEXT,
  n BIGINT, n_t BIGINT, n_s BIGINT, n_e BIGINT, n_l BIGINT, n_x BIGINT, n_ambiguous BIGINT,
  sum_net_ur NUMERIC, sum_sq_net_ur NUMERIC, sum_cost_ur NUMERIC, sum_fund_ur NUMERIC,
  first_exit_ms BIGINT, last_exit_ms BIGINT,
  placebo_n BIGINT, placebo_sum_net_ur NUMERIC, placebo_sum_sq_net_ur NUMERIC,
  exit_days BIGINT, cluster_sum_sq_ur NUMERIC, placebo_exit_days BIGINT, placebo_cluster_sum_sq_ur NUMERIC
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  WITH f AS (
    SELECT s.strategy_id, s.version, s.horizon_min, s.rr, o.status, o.net_ur, o.cost_ur, o.fund_ur, o.exit_ms
    FROM public.forward_market_outcomes o
    JOIN public.forward_market_setups s ON s.setup_id = o.setup_id
    WHERE o.final AND o.net_ur IS NOT NULL
      AND (p_from_ms IS NULL OR o.exit_ms >= p_from_ms)
      AND (p_to_ms IS NULL OR o.exit_ms < p_to_ms)
      AND (coalesce(cardinality(p_symbols), 0) = 0 OR s.symbol = ANY (p_symbols))
      AND (coalesce(cardinality(p_horizons), 0) = 0 OR s.horizon_min = ANY (p_horizons))
      AND (coalesce(cardinality(p_sides), 0) = 0 OR s.side = ANY (p_sides))
      AND (coalesce(cardinality(p_rr), 0) = 0 OR s.rr = ANY (p_rr))
  ),
  rl AS (
    SELECT f.strategy_id, f.version, f.horizon_min, f.rr,
           count(*) AS n,
           count(*) FILTER (WHERE f.status = 'T') AS n_t,
           count(*) FILTER (WHERE f.status = 'S') AS n_s,
           count(*) FILTER (WHERE f.status = 'E') AS n_e,
           count(*) FILTER (WHERE f.status = 'L') AS n_l,
           count(*) FILTER (WHERE f.status = 'X') AS n_x,
           count(*) FILTER (WHERE f.status = 'ambiguous') AS n_ambiguous,
           sum(f.net_ur::numeric) AS sum_net_ur,
           sum(f.net_ur::numeric * f.net_ur::numeric) AS sum_sq_net_ur,
           coalesce(sum(f.cost_ur::numeric), 0) AS sum_cost_ur,
           coalesce(sum(f.fund_ur::numeric), 0) AS sum_fund_ur,
           min(f.exit_ms) AS first_exit_ms, max(f.exit_ms) AS last_exit_ms
    FROM f
    WHERE f.strategy_id NOT LIKE 'placebo-v1:%'
      AND (coalesce(cardinality(p_strategy_ids), 0) = 0 OR f.strategy_id = ANY (p_strategy_ids))
    GROUP BY f.strategy_id, f.version, f.horizon_min, f.rr
  ),
  control AS (
    SELECT f.strategy_id, f.horizon_min, f.rr, count(*) AS n,
           sum(f.net_ur::numeric) AS sum_net_ur, sum(f.net_ur::numeric * f.net_ur::numeric) AS sum_sq_net_ur
    FROM f
    WHERE f.strategy_id LIKE 'placebo-v1:%'
    GROUP BY f.strategy_id, f.horizon_min, f.rr
  ),
  real_days AS (
    SELECT f.strategy_id, f.version, f.horizon_min, f.rr, f.exit_ms / 86400000 AS day,
           count(*) AS n, sum(f.net_ur::numeric) AS s
    FROM f WHERE f.exit_ms IS NOT NULL AND f.strategy_id NOT LIKE 'placebo-v1:%'
    GROUP BY f.strategy_id, f.version, f.horizon_min, f.rr, f.exit_ms / 86400000
  ),
  control_days AS (
    SELECT f.strategy_id, f.horizon_min, f.rr, f.exit_ms / 86400000 AS day,
           count(*) AS n, sum(f.net_ur::numeric) AS s
    FROM f WHERE f.exit_ms IS NOT NULL AND f.strategy_id LIKE 'placebo-v1:%'
    GROUP BY f.strategy_id, f.horizon_min, f.rr, f.exit_ms / 86400000
  ),
  real_clusters AS (
    SELECT r.strategy_id, r.version, r.horizon_min, r.rr, count(*) AS days,
           CASE WHEN sum(d.n) = r.n THEN sum(power(d.s - d.n * r.sum_net_ur / r.n, 2)) END AS ss
    FROM rl r JOIN real_days d USING (strategy_id, version, horizon_min, rr)
    GROUP BY r.strategy_id, r.version, r.horizon_min, r.rr, r.n, r.sum_net_ur
  ),
  control_clusters AS (
    SELECT c.strategy_id, c.horizon_min, c.rr, count(*) AS days,
           CASE WHEN sum(d.n) = c.n THEN sum(power(d.s - d.n * c.sum_net_ur / c.n, 2)) END AS ss
    FROM control c JOIN control_days d USING (strategy_id, horizon_min, rr)
    GROUP BY c.strategy_id, c.horizon_min, c.rr, c.n, c.sum_net_ur
  )
  SELECT r.strategy_id, r.version, r.horizon_min, r.rr,
         r.n, r.n_t, r.n_s, r.n_e, r.n_l, r.n_x, r.n_ambiguous,
         r.sum_net_ur, r.sum_sq_net_ur, r.sum_cost_ur, r.sum_fund_ur, r.first_exit_ms, r.last_exit_ms,
         coalesce(c.n, 0), coalesce(c.sum_net_ur, 0), coalesce(c.sum_sq_net_ur, 0),
         coalesce(rd.days, 0), rd.ss, coalesce(cd.days, 0), cd.ss
  FROM rl r
  LEFT JOIN control c
    ON c.strategy_id = 'placebo-v1:' || r.strategy_id AND c.horizon_min = r.horizon_min AND c.rr = r.rr
  LEFT JOIN real_clusters rd ON rd.strategy_id = r.strategy_id AND rd.version = r.version
    AND rd.horizon_min = r.horizon_min AND rd.rr = r.rr
  LEFT JOIN control_clusters cd
    ON cd.strategy_id = c.strategy_id AND cd.horizon_min = c.horizon_min AND cd.rr = c.rr
  ORDER BY r.n DESC, r.strategy_id, r.horizon_min, r.rr
$$;

CREATE OR REPLACE FUNCTION public.forward_paired_report(
  p_from_ms BIGINT, p_to_ms BIGINT, p_strategy_ids TEXT[], p_symbols TEXT[],
  p_horizons INTEGER[], p_sides INTEGER[], p_rr TEXT[]
)
RETURNS TABLE (
  strategy_id TEXT, version TEXT, horizon_min INTEGER, rr TEXT,
  paired_days BIGINT, union_days BIGINT, paired_ss NUMERIC
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  WITH f AS (
    SELECT s.strategy_id, s.version, s.horizon_min, s.rr, o.net_ur, o.exit_ms
    FROM public.forward_market_outcomes o
    JOIN public.forward_market_setups s ON s.setup_id = o.setup_id
    WHERE o.final AND o.net_ur IS NOT NULL AND o.exit_ms IS NOT NULL
      AND (p_from_ms IS NULL OR o.exit_ms >= p_from_ms)
      AND (p_to_ms IS NULL OR o.exit_ms < p_to_ms)
      AND (coalesce(cardinality(p_symbols), 0) = 0 OR s.symbol = ANY (p_symbols))
      AND (coalesce(cardinality(p_horizons), 0) = 0 OR s.horizon_min = ANY (p_horizons))
      AND (coalesce(cardinality(p_sides), 0) = 0 OR s.side = ANY (p_sides))
      AND (coalesce(cardinality(p_rr), 0) = 0 OR s.rr = ANY (p_rr))
  ),
  rd AS (
    SELECT f.strategy_id, f.version, f.horizon_min, f.rr, f.exit_ms / 86400000 AS day,
           count(*) AS n, sum(f.net_ur::numeric) AS s
    FROM f
    WHERE f.strategy_id NOT LIKE 'placebo-v1:%'
      AND (coalesce(cardinality(p_strategy_ids), 0) = 0 OR f.strategy_id = ANY (p_strategy_ids))
    GROUP BY f.strategy_id, f.version, f.horizon_min, f.rr, f.exit_ms / 86400000
  ),
  cd AS (
    SELECT f.strategy_id, f.horizon_min, f.rr, f.exit_ms / 86400000 AS day,
           count(*) AS n, sum(f.net_ur::numeric) AS s
    FROM f
    WHERE f.strategy_id LIKE 'placebo-v1:%'
    GROUP BY f.strategy_id, f.horizon_min, f.rr, f.exit_ms / 86400000
  ),
  rt AS (
    SELECT d.strategy_id, d.version, d.horizon_min, d.rr, sum(d.n) AS n, sum(d.s) AS s
    FROM rd d GROUP BY d.strategy_id, d.version, d.horizon_min, d.rr
  ),
  ct AS (
    SELECT d.strategy_id, d.horizon_min, d.rr, sum(d.n) AS n, sum(d.s) AS s
    FROM cd d GROUP BY d.strategy_id, d.horizon_min, d.rr
  ),
  k AS (
    SELECT r.strategy_id, r.version, r.horizon_min, r.rr, r.n AS rn, r.s AS rs, c.n AS cn, c.s AS cs
    FROM rt r JOIN ct c
      ON c.strategy_id = 'placebo-v1:' || r.strategy_id AND c.horizon_min = r.horizon_min AND c.rr = r.rr
  ),
  days AS (
    SELECT k.strategy_id, k.version, k.horizon_min, k.rr, u.day
    FROM k
    CROSS JOIN LATERAL (
      SELECT d.day FROM rd d
       WHERE d.strategy_id = k.strategy_id AND d.version = k.version AND d.horizon_min = k.horizon_min AND d.rr = k.rr
      UNION
      SELECT d.day FROM cd d
       WHERE d.strategy_id = 'placebo-v1:' || k.strategy_id AND d.horizon_min = k.horizon_min AND d.rr = k.rr
    ) u
  ),
  terms AS (
    SELECT k.strategy_id, k.version, k.horizon_min, k.rr,
           (a.n IS NOT NULL AND b.n IS NOT NULL) AS both_traded,
           (coalesce(a.s, 0) - coalesce(a.n, 0) * k.rs / k.rn) / k.rn
             - (coalesce(b.s, 0) - coalesce(b.n, 0) * k.cs / k.cn) / k.cn AS r
    FROM days dy
    JOIN k USING (strategy_id, version, horizon_min, rr)
    LEFT JOIN rd a ON a.strategy_id = k.strategy_id AND a.version = k.version AND a.horizon_min = k.horizon_min
      AND a.rr = k.rr AND a.day = dy.day
    LEFT JOIN cd b ON b.strategy_id = 'placebo-v1:' || k.strategy_id AND b.horizon_min = k.horizon_min
      AND b.rr = k.rr AND b.day = dy.day
  )
  SELECT t.strategy_id, t.version, t.horizon_min, t.rr,
         count(*) FILTER (WHERE t.both_traded), count(*), sum(t.r * t.r)
  FROM terms t
  GROUP BY t.strategy_id, t.version, t.horizon_min, t.rr
$$;

CREATE OR REPLACE FUNCTION public.forward_nontrade_report(
  p_from_ms BIGINT, p_to_ms BIGINT, p_strategy_ids TEXT[], p_symbols TEXT[],
  p_horizons INTEGER[], p_sides INTEGER[], p_rr TEXT[]
)
RETURNS TABLE (
  strategy_id TEXT, horizon_min INTEGER, rr TEXT,
  n_v BIGINT, n_c BIGINT, n_p BIGINT, n_g BIGINT, n_n BIGINT, pending_or_open BIGINT
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  SELECT s.strategy_id, s.horizon_min, s.rr,
         count(*) FILTER (WHERE s.status = 'V'),
         count(*) FILTER (WHERE s.status = 'C'),
         count(*) FILTER (WHERE s.status = 'P'),
         count(*) FILTER (WHERE s.status = 'G'),
         count(*) FILTER (WHERE s.status = 'N'),
         count(*) FILTER (WHERE s.status = 'T' AND NOT EXISTS (
           SELECT 1 FROM public.forward_market_outcomes o
           WHERE o.setup_id = s.setup_id AND o.final))
  FROM public.forward_market_setups s
  WHERE s.strategy_id NOT LIKE 'placebo-v1:%'
    AND (p_from_ms IS NULL OR s.entry_ms >= p_from_ms)
    AND (p_to_ms IS NULL OR s.entry_ms < p_to_ms)
    AND (coalesce(cardinality(p_strategy_ids), 0) = 0 OR s.strategy_id = ANY (p_strategy_ids))
    AND (coalesce(cardinality(p_symbols), 0) = 0 OR s.symbol = ANY (p_symbols))
    AND (coalesce(cardinality(p_horizons), 0) = 0 OR s.horizon_min = ANY (p_horizons))
    AND (coalesce(cardinality(p_sides), 0) = 0 OR s.side = ANY (p_sides))
    AND (coalesce(cardinality(p_rr), 0) = 0 OR s.rr = ANY (p_rr))
  GROUP BY s.strategy_id, s.horizon_min, s.rr
$$;

CREATE OR REPLACE FUNCTION public.paper_equity_series(p_bucket_ms BIGINT DEFAULT 3600000, p_from_ms BIGINT DEFAULT NULL)
RETURNS TABLE (bucket_ms BIGINT, ms BIGINT, seq BIGINT, balance_e8 BIGINT)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  SELECT DISTINCT ON (l.ms / b.size) (l.ms / b.size) * b.size, l.ms, l.seq, l.balance_e8
  FROM public.paper_ledger l,
       (SELECT CASE WHEN p_bucket_ms > 0 THEN p_bucket_ms ELSE 3600000 END AS size) b
  WHERE EXISTS (SELECT 1 FROM public.forward_wallet_accounts a WHERE a.user_id=l.user_id AND a.account_id=l.paper_account) AND (p_from_ms IS NULL OR l.ms >= p_from_ms)
  ORDER BY l.ms / b.size, l.seq DESC
$$;

-- Keep the self-check RPC signatures, but report shared market counts once (NULL owner).
CREATE OR REPLACE FUNCTION public.forward_state_counts()
RETURNS TABLE (user_id UUID, kind TEXT, status TEXT, n BIGINT)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path=public AS $$
  SELECT NULL::UUID,'signals'::TEXT,''::TEXT,count(*) FROM public.forward_market_signals
  UNION ALL SELECT NULL::UUID,'setups',s.status,count(*) FROM public.forward_market_setups s GROUP BY s.status
  UNION ALL SELECT NULL::UUID,'outcomes',o.status,count(*) FROM public.forward_market_outcomes o
    WHERE o.final GROUP BY o.status
$$;
CREATE OR REPLACE FUNCTION public.forward_recent_strategies(p_since_ms BIGINT)
RETURNS TABLE (user_id UUID, strategy_id TEXT, n BIGINT)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path=public AS $$
  SELECT NULL::UUID,s.strategy_id,count(*) FROM public.forward_market_signals s
    WHERE s.signal_ms>=p_since_ms GROUP BY s.strategy_id
$$;
