-- #374 Forex database. Three parts, all additive for crypto:
--   1a. Access: roles/plans decide who may SEE which market (the backend always computes both).
--   1b. Forex market facts: shared, append-only tables with their OWN stage cursor and advisory lock, so a forex
--       stage and a crypto stage for the same hour never collide or block each other.
--   1c. Per-market paper accounts: forward_wallet_accounts is keyed by (user_id, market); the forex account id is
--       '<user uuid>:forex'. The two paper reports gain p_market (default 'crypto') so markets are never summed.
-- No user, email or id is seeded here (public repo); grant access with `npm run access:grant`.

-- ---------------------------------------------------------------------------------------------------------------
-- 1a. Access
-- ---------------------------------------------------------------------------------------------------------------
CREATE TABLE public.plans (
  plan TEXT PRIMARY KEY CHECK (plan ~ '^[a-z][a-z0-9_-]{1,31}$'),
  label TEXT NOT NULL
);
CREATE TABLE public.plan_markets (
  plan TEXT NOT NULL REFERENCES public.plans,
  market TEXT NOT NULL CHECK (market IN ('crypto','forex')),
  PRIMARY KEY(plan,market)
);
CREATE TABLE public.user_access (
  user_id UUID PRIMARY KEY REFERENCES auth.users ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner','admin','member')),
  plan TEXT NOT NULL DEFAULT 'free' REFERENCES public.plans,
  extra_markets TEXT[] NOT NULL DEFAULT '{}' CHECK (extra_markets <@ ARRAY['crypto','forex']::TEXT[]),
  expires_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO public.plans(plan,label) VALUES
  ('free','Free'),('crypto','Crypto'),('forex','Forex'),('pro','Pro');
INSERT INTO public.plan_markets(plan,market) VALUES
  ('free','crypto'),('crypto','crypto'),('forex','forex'),('pro','crypto'),('pro','forex');

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['plans','plan_markets','user_access'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC,anon,authenticated',t);
    EXECUTE format('GRANT SELECT ON public.%I TO authenticated',t);
    EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON public.%I TO service_role',t);
  END LOOP;
END $$;
CREATE POLICY plans_readable ON public.plans FOR SELECT TO authenticated USING (true);
CREATE POLICY plan_markets_readable ON public.plan_markets FOR SELECT TO authenticated USING (true);
CREATE POLICY own_access ON public.user_access FOR SELECT TO authenticated USING (auth.uid()=user_id);

-- No row: crypto only (everyone keeps today's behaviour). Owner/admin: both. Otherwise the plan's markets (an
-- expired plan counts as 'free') plus extra_markets. Sorted, distinct.
CREATE FUNCTION public.markets_of(p_user UUID)
RETURNS TEXT[] LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public AS $$
  SELECT CASE
    WHEN ua.user_id IS NULL THEN ARRAY['crypto']::TEXT[]
    WHEN ua.role IN ('owner','admin') THEN ARRAY['crypto','forex']::TEXT[]
    ELSE coalesce((SELECT array_agg(DISTINCT x.m ORDER BY x.m) FROM (
        SELECT pm.market AS m FROM public.plan_markets pm
          WHERE pm.plan = CASE WHEN ua.expires_at IS NOT NULL AND ua.expires_at <= now() THEN 'free' ELSE ua.plan END
        UNION SELECT unnest(ua.extra_markets)) x), ARRAY[]::TEXT[])
  END
  FROM (SELECT 1) one LEFT JOIN public.user_access ua ON ua.user_id = p_user
$$;
REVOKE ALL ON FUNCTION public.markets_of(UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.markets_of(UUID) TO service_role;

CREATE FUNCTION public.my_markets()
RETURNS TEXT[] LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public AS $$
  SELECT CASE WHEN auth.uid() IS NULL THEN ARRAY[]::TEXT[] ELSE public.markets_of(auth.uid()) END
$$;
CREATE FUNCTION public.has_market(p_market TEXT)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public AS $$
  SELECT p_market = ANY(public.my_markets())
$$;
REVOKE ALL ON FUNCTION public.my_markets(),public.has_market(TEXT) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.my_markets(),public.has_market(TEXT) TO authenticated,service_role;

-- ---------------------------------------------------------------------------------------------------------------
-- 1b. Forex market facts (pattern: 20261011150000_forward_shared_stages.sql, independent tables and cursor)
-- ---------------------------------------------------------------------------------------------------------------
CREATE TABLE public.fx_market_stages (
  id UUID PRIMARY KEY,
  boundary_ms BIGINT NOT NULL UNIQUE CHECK (boundary_ms >= 0),
  versions JSONB NOT NULL,
  params_hash TEXT,
  payload JSONB NOT NULL,
  freshness JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.fx_market_signals (
  signal_id TEXT PRIMARY KEY CHECK (signal_id ~ '^[0-9a-f]{64}$'),
  strategy_id TEXT NOT NULL CHECK (length(strategy_id) BETWEEN 1 AND 128),
  version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
  symbol TEXT NOT NULL CHECK (length(symbol) BETWEEN 3 AND 16),
  instrument_id TEXT NOT NULL,
  signal_ms BIGINT NOT NULL CHECK (signal_ms >= 0),
  side SMALLINT NOT NULL CHECK (side IN (-1, 1)),
  horizon_min INTEGER NOT NULL CHECK (horizon_min IN (15, 60, 240)),
  sessions TEXT[] NOT NULL DEFAULT '{}',
  candle_version TEXT CHECK (candle_version IS NULL OR candle_version ~ '^[0-9a-f]{64}$'),
  stage_run_id UUID NOT NULL REFERENCES public.fx_market_stages,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.fx_market_suppressed (
  signal_id TEXT PRIMARY KEY CHECK (signal_id ~ '^[0-9a-f]{64}$'),
  strategy_id TEXT NOT NULL CHECK (length(strategy_id) BETWEEN 1 AND 128),
  version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
  symbol TEXT NOT NULL CHECK (length(symbol) BETWEEN 3 AND 16),
  instrument_id TEXT NOT NULL,
  signal_ms BIGINT NOT NULL CHECK (signal_ms >= 0),
  side SMALLINT NOT NULL CHECK (side IN (-1, 1)),
  horizon_min INTEGER NOT NULL CHECK (horizon_min IN (15, 60, 240)),
  sessions TEXT[] NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL CHECK (reason IN ('market_closed_at_entry','entry_minute_missing','window_exceeds_session',
                                         'news_blackout')),
  detail JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail) = 'object'),
  stage_run_id UUID NOT NULL REFERENCES public.fx_market_stages,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.fx_market_setups (
  setup_id TEXT PRIMARY KEY CHECK (setup_id ~ '^[0-9a-f]{64}$'),
  signal_id TEXT NOT NULL REFERENCES public.fx_market_signals,
  strategy_id TEXT NOT NULL,
  version TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side SMALLINT NOT NULL CHECK (side IN (-1, 1)),
  horizon_min INTEGER NOT NULL,
  signal_ms BIGINT NOT NULL,
  entry_ms BIGINT NOT NULL,
  k TEXT NOT NULL,
  rr TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('T', 'V', 'C', 'P', 'G', 'N')),
  geometry_version TEXT NOT NULL,
  params_hash TEXT NOT NULL CHECK (length(params_hash) = 64),
  payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  stage_run_id UUID NOT NULL REFERENCES public.fx_market_stages,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.fx_market_outcomes (
  setup_id TEXT NOT NULL REFERENCES public.fx_market_setups,
  status TEXT NOT NULL CHECK (status IN ('pending','open','T','S','E','L','X','ambiguous')),
  final BOOLEAN NOT NULL,
  resolved_through_ms BIGINT,
  exit_ms BIGINT,
  net_ur BIGINT,
  cost_ur BIGINT,
  fund_ur BIGINT,
  payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  stage_run_id UUID NOT NULL REFERENCES public.fx_market_stages,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(setup_id,status)
);
CREATE TABLE public.fx_market_events (
  stage_run_id UUID NOT NULL REFERENCES public.fx_market_stages,
  symbol TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence >= 0),
  ms BIGINT NOT NULL CHECK (ms >= 0),
  type TEXT NOT NULL CHECK (type IN ('open','close','swap','liquidation')),
  payload JSONB NOT NULL,
  PRIMARY KEY(stage_run_id,symbol,sequence)
);
CREATE INDEX fx_market_setups_entry ON public.fx_market_setups(entry_ms DESC);
CREATE INDEX fx_market_signals_time ON public.fx_market_signals(signal_ms DESC);
CREATE INDEX fx_market_suppressed_time ON public.fx_market_suppressed(signal_ms DESC);
CREATE INDEX fx_market_events_time ON public.fx_market_events(stage_run_id,ms,symbol,sequence);

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['fx_market_stages','fx_market_signals','fx_market_suppressed','fx_market_setups',
    'fx_market_outcomes','fx_market_events'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC,anon,authenticated',t);
    EXECUTE format('GRANT SELECT ON public.%I TO authenticated',t);
    EXECUTE format('GRANT SELECT,INSERT ON public.%I TO service_role',t);
    EXECUTE format('CREATE POLICY fx_entitled ON public.%I FOR SELECT TO authenticated USING (public.has_market(''forex''))',t);
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.reject_forward_mutation()',t||'_append_only',t);
  END LOOP;
END $$;

CREATE VIEW public.fx_market_open_setups WITH (security_invoker=true) AS
SELECT s.* FROM public.fx_market_setups s WHERE s.status='T' AND NOT EXISTS
  (SELECT 1 FROM public.fx_market_outcomes o WHERE o.setup_id=s.setup_id AND o.final);
CREATE VIEW public.fx_market_outcome_log WITH (security_invoker=true) AS
SELECT s.setup_id,s.strategy_id,s.version,s.symbol,s.side,s.horizon_min,s.rr,s.entry_ms,
  o.status,o.net_ur,o.cost_ur,o.fund_ur,o.exit_ms
FROM public.fx_market_setups s JOIN public.fx_market_outcomes o ON o.setup_id=s.setup_id AND o.final;
REVOKE ALL ON public.fx_market_open_setups,public.fx_market_outcome_log FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.fx_market_open_setups,public.fx_market_outcome_log TO authenticated,service_role;

-- commit_market_stage without the sigma parts, under its own lock key and cursor.
CREATE FUNCTION public.commit_fx_stage(p_stage JSONB,p_records JSONB)
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
        USING item || jsonb_build_object('stage_run_id',stage_id,'created_at',clock_timestamp());
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

-- ---------------------------------------------------------------------------------------------------------------
-- 1c. Per-market paper accounts
-- ---------------------------------------------------------------------------------------------------------------
ALTER TABLE public.forward_wallet_accounts
  ADD COLUMN market TEXT NOT NULL DEFAULT 'crypto' CHECK (market IN ('crypto','forex'));
DO $$
DECLARE
  existing TEXT;
BEGIN
  SELECT conname INTO existing
  FROM pg_constraint
  WHERE conrelid = 'public.forward_wallet_accounts'::regclass AND contype = 'p';
  IF existing IS NOT NULL THEN
    EXECUTE format('ALTER TABLE public.forward_wallet_accounts DROP CONSTRAINT %I', existing);
  END IF;
END;
$$;
ALTER TABLE public.forward_wallet_accounts ADD PRIMARY KEY (user_id,market);
CREATE UNIQUE INDEX forward_wallet_accounts_account ON public.forward_wallet_accounts(account_id);

ALTER TABLE public.paper_runs
  ADD COLUMN market TEXT NOT NULL DEFAULT 'crypto' CHECK (market IN ('crypto','forex')),
  ADD COLUMN fx_stage_run_id UUID REFERENCES public.fx_market_stages,
  ADD CONSTRAINT paper_runs_market_stage CHECK ((market='crypto' AND fx_stage_run_id IS NULL)
                                                OR (market='forex' AND stage_run_id IS NULL));
CREATE UNIQUE INDEX paper_runs_fx_hour ON public.paper_runs(user_id,boundary_ms)
  WHERE fx_stage_run_id IS NOT NULL AND status='ok';

-- The crypto wallet commit read its account with only user_id; with a forex row next to it that would pick an
-- arbitrary one. Same body as 20261011150000_forward_shared_stages.sql except the account filter.
CREATE OR REPLACE FUNCTION public.commit_user_run(p_user_id UUID,p_stage_run_id UUID,p_run JSONB,p_ledger JSONB,p_expected_run_id UUID)
RETURNS UUID LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE account public.forward_wallet_accounts; stage public.forward_market_stages;
  latest public.paper_runs; existing UUID; run_id UUID := gen_random_uuid(); cursor_ms BIGINT; item JSONB;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-wallet:'||p_user_id::TEXT,0));
  SELECT * INTO account FROM public.forward_wallet_accounts WHERE user_id=p_user_id AND market='crypto';
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
REVOKE ALL ON FUNCTION public.commit_user_run(UUID,UUID,JSONB,JSONB,UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.commit_user_run(UUID,UUID,JSONB,JSONB,UUID) TO service_role;

CREATE FUNCTION public.ensure_fx_account(p_user_id UUID,p_boundary_ms BIGINT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-wallet:'||p_user_id::TEXT||':forex',0));
  IF NOT ('forex' = ANY(public.markets_of(p_user_id))) THEN
    RAISE EXCEPTION 'User is not entitled to forex' USING ERRCODE='42501';
  END IF;
  INSERT INTO public.forward_wallet_accounts(user_id,market,account_id,start_boundary_ms)
    VALUES(p_user_id,'forex',p_user_id::TEXT||':forex',p_boundary_ms) ON CONFLICT DO NOTHING;
END $$;

-- commit_user_run for the forex account: own lock key, fx stages, fx cursor, fx_stage_run_id.
CREATE FUNCTION public.commit_fx_user_run(p_user_id UUID,p_stage_run_id UUID,p_run JSONB,p_ledger JSONB,p_expected_run_id UUID)
RETURNS UUID LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE account public.forward_wallet_accounts; stage public.fx_market_stages;
  latest public.paper_runs; existing UUID; run_id UUID := gen_random_uuid(); cursor_ms BIGINT; item JSONB;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-wallet:'||p_user_id::TEXT||':forex',0));
  SELECT * INTO account FROM public.forward_wallet_accounts WHERE user_id=p_user_id AND market='forex';
  IF account.user_id IS NULL THEN RAISE EXCEPTION 'Account not initialized'; END IF;
  SELECT * INTO stage FROM public.fx_market_stages WHERE id=p_stage_run_id;
  IF stage.id IS NULL THEN RAISE EXCEPTION 'Market stage missing'; END IF;
  SELECT id INTO existing FROM public.paper_runs
    WHERE user_id=p_user_id AND market='forex' AND fx_stage_run_id=stage.id AND status='ok';
  IF existing IS NOT NULL THEN RETURN existing; END IF;
  SELECT * INTO latest FROM public.paper_runs
    WHERE user_id=p_user_id AND market='forex' AND paper_account=account.account_id AND status='ok'
    ORDER BY boundary_ms DESC,created_at DESC,id DESC LIMIT 1;
  IF latest.id IS DISTINCT FROM p_expected_run_id THEN
    RAISE EXCEPTION 'Wallet changed; reload and retry' USING ERRCODE='40001';
  END IF;
  cursor_ms := greatest(account.start_boundary_ms,coalesce(latest.consumed_boundary_ms,latest.boundary_ms,0));
  IF stage.boundary_ms<=cursor_ms OR EXISTS(SELECT 1 FROM public.fx_market_stages
      WHERE boundary_ms>cursor_ms AND boundary_ms<stage.boundary_ms) THEN
    RAISE EXCEPTION 'Wallet stages must be consumed in order';
  END IF;
  IF p_run->>'status' NOT IN ('ok','skipped_stale','no_sigma') THEN RAISE EXCEPTION 'Invalid user run status'; END IF;
  IF p_run->>'status'<>'ok' AND jsonb_array_length(p_ledger)>0 THEN RAISE EXCEPTION 'Retryable ledger must be empty'; END IF;
  INSERT INTO public.paper_runs SELECT (jsonb_populate_record(NULL::public.paper_runs,
    jsonb_build_object('processed_to_ms','{}'::jsonb,'versions','{}'::jsonb,'assumptions','[]'::jsonb,
      'reasons','{}'::jsonb,'counts','{}'::jsonb,'freshness','{}'::jsonb) || p_run ||
    jsonb_build_object('id',run_id,'user_id',p_user_id,'paper_account',account.account_id,
      'market','forex','fx_stage_run_id',stage.id,'stage_run_id',NULL,
      'boundary_ms',stage.boundary_ms,'run_key','fx-hour:'||stage.boundary_ms,
      'consumed_boundary_ms',CASE WHEN p_run->>'status'='ok' THEN stage.boundary_ms ELSE NULL END,
      'created_at',clock_timestamp()))).*;
  FOR item IN SELECT value FROM jsonb_array_elements(p_ledger) LOOP
    INSERT INTO public.paper_ledger(user_id,paper_account,seq,ms,type,setup_id,symbol,amount_e8,balance_e8,payload,run_id)
      VALUES(p_user_id,account.account_id,(item->>'seq')::BIGINT,(item->>'ms')::BIGINT,item->>'type',
        item->>'setup_id',item->>'symbol',(item->>'amount_e8')::BIGINT,(item->>'balance_e8')::BIGINT,item,run_id);
  END LOOP;
  RETURN run_id;
END $$;
REVOKE ALL ON FUNCTION public.ensure_fx_account(UUID,BIGINT),
  public.commit_fx_user_run(UUID,UUID,JSONB,JSONB,UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.ensure_fx_account(UUID,BIGINT),
  public.commit_fx_user_run(UUID,UUID,JSONB,JSONB,UUID) TO service_role;

-- ---------------------------------------------------------------------------------------------------------------
-- 2. The two paper reports gain p_market (default 'crypto'), so two accounts of one user are never summed.
--    Dropped first: CREATE OR REPLACE with a new parameter would add an overload and make the no-arg call ambiguous.
-- ---------------------------------------------------------------------------------------------------------------
DROP FUNCTION public.paper_equity_series(BIGINT, BIGINT);
CREATE FUNCTION public.paper_equity_series(p_bucket_ms BIGINT DEFAULT 3600000, p_from_ms BIGINT DEFAULT NULL,
                                           p_market TEXT DEFAULT 'crypto')
RETURNS TABLE (bucket_ms BIGINT, ms BIGINT, seq BIGINT, balance_e8 BIGINT)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  SELECT DISTINCT ON (l.ms / b.size) (l.ms / b.size) * b.size, l.ms, l.seq, l.balance_e8
  FROM public.paper_ledger l,
       (SELECT CASE WHEN p_bucket_ms > 0 THEN p_bucket_ms ELSE 3600000 END AS size) b
  WHERE (p_market = 'crypto' OR public.has_market(p_market)) AND EXISTS (SELECT 1 FROM public.forward_wallet_accounts a WHERE a.user_id = l.user_id AND a.account_id = l.paper_account AND a.market = p_market) AND (p_from_ms IS NULL OR l.ms >= p_from_ms)
  ORDER BY l.ms / b.size, l.seq DESC
$$;
REVOKE ALL ON FUNCTION public.paper_equity_series(BIGINT, BIGINT, TEXT) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.paper_equity_series(BIGINT, BIGINT, TEXT) TO authenticated;

DROP FUNCTION public.paper_account_summary();
CREATE FUNCTION public.paper_account_summary(p_market TEXT DEFAULT 'crypto')
RETURNS TABLE (
  trades BIGINT, target BIGINT, stopped BIGINT, expired BIGINT, liquidated BIGINT, ambiguous BIGINT,
  unresolved BIGINT, wins BIGINT, losses BIGINT,
  sum_net_e8 NUMERIC, gross_wins_e8 NUMERIC, gross_losses_e8 NUMERIC,
  rejected BIGINT, order_rejected BIGINT,
  open_fees_e8 NUMERIC, close_fees_e8 NUMERIC, funding_e8 NUMERIC, liquidation_e8 NUMERIC, pnl_e8 NUMERIC,
  lines BIGINT, first_ms BIGINT, last_ms BIGINT
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  WITH acct AS (
    SELECT l.*
    FROM public.paper_ledger l
    WHERE (p_market = 'crypto' OR public.has_market(p_market))
      AND EXISTS (SELECT 1 FROM public.forward_wallet_accounts a
                  WHERE a.user_id = l.user_id AND a.account_id = l.paper_account AND a.market = p_market)
  ),
  closing AS (
    SELECT DISTINCT ON (c.setup_id) c.setup_id, c.type, c.payload ->> 'outcome' AS outcome
    FROM acct c
    WHERE c.setup_id IS NOT NULL AND c.type IN ('pnl', 'liquidation')
      AND (c.payload ->> 'position_event' IS NULL OR c.payload ->> 'position_event' = 'closed')
    ORDER BY c.setup_id, c.seq DESC
  ),
  trade AS (
    SELECT c.setup_id, c.type, c.outcome, sum(m.amount_e8) AS net
    FROM closing c
    JOIN acct m ON m.setup_id = c.setup_id
               AND m.type IN ('open_fee', 'pnl', 'close_fee', 'funding', 'liquidation')
    GROUP BY c.setup_id, c.type, c.outcome
  )
  SELECT
    (SELECT count(*) FROM trade),
    (SELECT count(*) FROM trade WHERE type = 'pnl' AND outcome = 'T'),
    (SELECT count(*) FROM trade WHERE type = 'pnl' AND outcome = 'S'),
    (SELECT count(*) FROM trade WHERE type = 'pnl' AND outcome = 'E'),
    (SELECT count(*) FROM trade WHERE type = 'liquidation'),
    (SELECT count(*) FROM trade WHERE type = 'pnl' AND outcome = 'ambiguous'),
    (SELECT count(*) FROM trade WHERE type = 'pnl' AND (outcome IS NULL OR outcome NOT IN ('T', 'S', 'E', 'ambiguous'))),
    (SELECT count(*) FROM trade WHERE net > 0),
    (SELECT count(*) FROM trade WHERE net < 0),
    (SELECT coalesce(sum(net), 0) FROM trade),
    (SELECT coalesce(sum(net) FILTER (WHERE net > 0), 0) FROM trade),
    (SELECT coalesce(sum(net) FILTER (WHERE net < 0), 0) FROM trade),
    (SELECT count(*) FROM acct WHERE type = 'rejected'),
    (SELECT count(*) FROM acct WHERE type = 'order_rejected'),
    (SELECT coalesce(sum(amount_e8), 0) FROM acct WHERE type = 'open_fee'),
    (SELECT coalesce(sum(amount_e8), 0) FROM acct WHERE type = 'close_fee'),
    (SELECT coalesce(sum(amount_e8), 0) FROM acct WHERE type = 'funding'),
    (SELECT coalesce(sum(amount_e8), 0) FROM acct WHERE type = 'liquidation'),
    (SELECT coalesce(sum(amount_e8), 0) FROM acct WHERE type = 'pnl'),
    (SELECT count(*) FROM acct),
    (SELECT min(ms) FROM acct),
    (SELECT max(ms) FROM acct)
$$;
REVOKE ALL ON FUNCTION public.paper_account_summary(TEXT) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.paper_account_summary(TEXT) TO authenticated;
