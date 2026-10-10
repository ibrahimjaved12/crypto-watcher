-- Forward harness (#239 P11): permanent, account-scoped forward-test records.
-- Every table is append-only (UPDATE/DELETE rejected); idempotency comes from natural keys
-- (signal_id, setup_id, (setup_id, status), (paper_account, seq), run_key), so a re-run inserts
-- nothing new. Rows are written by the server (service role) and read by their owner (RLS).

CREATE FUNCTION public.reject_forward_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
  RAISE EXCEPTION 'forward records are append-only'
    USING ERRCODE = '55000';
END;
$$;

CREATE TABLE public.paper_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  paper_account TEXT NOT NULL DEFAULT 'default' CHECK (length(paper_account) BETWEEN 1 AND 64),
  run_key TEXT NOT NULL CHECK (length(run_key) BETWEEN 1 AND 128),
  trigger TEXT NOT NULL CHECK (trigger IN ('hourly', 'on_demand')),
  status TEXT NOT NULL CHECK (status IN ('ok', 'skipped_stale', 'failed')),
  reason TEXT CHECK (reason IS NULL OR length(reason) <= 2000),
  boundary_ms BIGINT NOT NULL CHECK (boundary_ms >= 0),
  from_ms BIGINT CHECK (from_ms >= 0),
  processed_to_ms JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(processed_to_ms) = 'object'),
  versions JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(versions) = 'object'),
  params_hash TEXT,
  wallet_config JSONB,
  wallet_state JSONB,
  assumptions JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(assumptions) = 'array'),
  reasons JSONB NOT NULL DEFAULT '{}'::jsonb,
  counts JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(counts) = 'object'),
  freshness JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(freshness) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (user_id, paper_account, run_key)
);
CREATE INDEX paper_runs_latest ON public.paper_runs(user_id, paper_account, boundary_ms DESC);

CREATE TABLE public.forward_signals (
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  signal_id TEXT NOT NULL CHECK (signal_id ~ '^[0-9a-f]{64}$'),
  strategy_id TEXT NOT NULL CHECK (length(strategy_id) BETWEEN 1 AND 128),
  version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
  symbol TEXT NOT NULL CHECK (length(symbol) BETWEEN 5 AND 16),
  signal_ms BIGINT NOT NULL CHECK (signal_ms >= 0),
  side SMALLINT NOT NULL CHECK (side IN (-1, 1)),
  horizon_min INTEGER NOT NULL CHECK (horizon_min IN (15, 60, 240)),
  candle_version TEXT CHECK (candle_version IS NULL OR candle_version ~ '^[0-9a-f]{64}$'),
  run_id UUID NOT NULL REFERENCES public.paper_runs ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id, signal_id)
);
CREATE INDEX forward_signals_recent ON public.forward_signals(user_id, signal_ms DESC);

CREATE TABLE public.forward_setups (
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  setup_id TEXT NOT NULL CHECK (setup_id ~ '^[0-9a-f]{64}$'),
  signal_id TEXT NOT NULL,
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
  params_hash TEXT NOT NULL CHECK (length(params_hash) = 64),
  payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  candle_version TEXT CHECK (candle_version IS NULL OR candle_version ~ '^[0-9a-f]{64}$'),
  run_id UUID NOT NULL REFERENCES public.paper_runs ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id, setup_id),
  FOREIGN KEY (user_id, signal_id) REFERENCES public.forward_signals (user_id, signal_id)
);
CREATE INDEX forward_setups_recent ON public.forward_setups(user_id, entry_ms DESC);
CREATE INDEX forward_setups_by_strategy ON public.forward_setups(user_id, strategy_id, entry_ms DESC);

CREATE TABLE public.forward_outcomes (
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  setup_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'open', 'T', 'S', 'E', 'L', 'X', 'ambiguous')),
  final BOOLEAN NOT NULL,
  resolved_through_ms BIGINT,
  exit_ms BIGINT,
  net_ur BIGINT,
  cost_ur BIGINT,
  fund_ur BIGINT,
  payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  run_id UUID NOT NULL REFERENCES public.paper_runs ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id, setup_id, status),
  FOREIGN KEY (user_id, setup_id) REFERENCES public.forward_setups (user_id, setup_id)
);
CREATE INDEX forward_outcomes_final ON public.forward_outcomes(user_id, final, exit_ms DESC);

CREATE TABLE public.paper_ledger (
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  paper_account TEXT NOT NULL DEFAULT 'default',
  seq BIGINT NOT NULL CHECK (seq >= 1),
  ms BIGINT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('open_fee', 'pnl', 'close_fee', 'funding', 'liquidation', 'rejected')),
  setup_id TEXT,
  symbol TEXT,
  amount_e8 BIGINT NOT NULL,
  balance_e8 BIGINT NOT NULL,
  payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  run_id UUID NOT NULL REFERENCES public.paper_runs ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id, paper_account, seq)
);

DO $$
DECLARE
  name TEXT;
BEGIN
  FOREACH name IN ARRAY ARRAY['paper_runs', 'forward_signals', 'forward_setups', 'forward_outcomes', 'paper_ledger']
  LOOP
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

COMMENT ON TABLE public.forward_setups IS
  'Immutable forward-test setups (labels-v2 geometry, #239). No strategy has a validated edge.';

-- Candle conflict policy (#239 P15): candle_version is the SHA-256 over the operational
-- collector_candle_hash values (candle-v1) of the completed 1m candles forming the decision candle,
-- so a later REST revision can be traced to the decisions that used the original candle.
COMMENT ON COLUMN public.forward_signals.candle_version IS
  'SHA-256 of the candle-v1 hashes of the 1m candles forming the decision candle (P15); NULL when unknown.';
COMMENT ON COLUMN public.forward_setups.candle_version IS
  'candle_version of the setup''s signal (P15); NULL when unknown.';

-- Honest reporting (P20): every aggregate is computed in SQL over ALL rows (no client-side row cap),
-- from the caller's own rows only (SECURITY INVOKER + RLS). NULL or empty filter arrays mean "all".
-- Outcome time is `exit_ms`; setup time is `entry_ms`; the range is [p_from_ms, p_to_ms).

CREATE FUNCTION public.forward_outcome_report(
  p_from_ms BIGINT, p_to_ms BIGINT, p_strategy_ids TEXT[], p_symbols TEXT[],
  p_horizons INTEGER[], p_sides INTEGER[], p_rr TEXT[]
)
RETURNS TABLE (
  strategy_id TEXT, version TEXT, horizon_min INTEGER, rr TEXT,
  n BIGINT, n_t BIGINT, n_s BIGINT, n_e BIGINT, n_l BIGINT, n_x BIGINT, n_ambiguous BIGINT,
  sum_net_ur NUMERIC, sum_sq_net_ur NUMERIC, sum_cost_ur NUMERIC, sum_fund_ur NUMERIC,
  first_exit_ms BIGINT, last_exit_ms BIGINT,
  placebo_n BIGINT, placebo_sum_net_ur NUMERIC, placebo_sum_sq_net_ur NUMERIC
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  WITH f AS (
    SELECT s.strategy_id, s.version, s.horizon_min, s.rr, o.status, o.net_ur, o.cost_ur, o.fund_ur, o.exit_ms
    FROM public.forward_outcomes o
    JOIN public.forward_setups s ON s.user_id = o.user_id AND s.setup_id = o.setup_id
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
  )
  SELECT r.strategy_id, r.version, r.horizon_min, r.rr,
         r.n, r.n_t, r.n_s, r.n_e, r.n_l, r.n_x, r.n_ambiguous,
         r.sum_net_ur, r.sum_sq_net_ur, r.sum_cost_ur, r.sum_fund_ur, r.first_exit_ms, r.last_exit_ms,
         coalesce(c.n, 0), coalesce(c.sum_net_ur, 0), coalesce(c.sum_sq_net_ur, 0)
  FROM rl r
  LEFT JOIN control c
    ON c.strategy_id = 'placebo-v1:' || r.strategy_id AND c.horizon_min = r.horizon_min AND c.rr = r.rr
  ORDER BY r.n DESC, r.strategy_id, r.horizon_min, r.rr
$$;

CREATE FUNCTION public.forward_nontrade_report(
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
           SELECT 1 FROM public.forward_outcomes o
           WHERE o.user_id = s.user_id AND o.setup_id = s.setup_id AND o.final))
  FROM public.forward_setups s
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

-- Last ledger line per time bucket, ascending, for the equity curve. Bucketing bounds the result
-- (hourly, or daily for long spans) so there is no row cap and no truncated history.
CREATE FUNCTION public.paper_equity_series(p_bucket_ms BIGINT DEFAULT 3600000, p_from_ms BIGINT DEFAULT NULL)
RETURNS TABLE (bucket_ms BIGINT, ms BIGINT, seq BIGINT, balance_e8 BIGINT)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  SELECT DISTINCT ON (l.ms / b.size) (l.ms / b.size) * b.size, l.ms, l.seq, l.balance_e8
  FROM public.paper_ledger l,
       (SELECT CASE WHEN p_bucket_ms > 0 THEN p_bucket_ms ELSE 3600000 END AS size) b
  WHERE l.paper_account = 'default' AND (p_from_ms IS NULL OR l.ms >= p_from_ms)
  ORDER BY l.ms / b.size, l.seq DESC
$$;

-- One row per setup with its FINAL outcome, for the paginated outcome log.
CREATE VIEW public.forward_outcome_log WITH (security_invoker = true) AS
  SELECT s.user_id, s.setup_id, s.strategy_id, s.version, s.symbol, s.side, s.horizon_min, s.rr,
         s.entry_ms, o.status, o.net_ur, o.cost_ur, o.fund_ur, o.exit_ms
  FROM public.forward_setups s
  JOIN public.forward_outcomes o ON o.user_id = s.user_id AND o.setup_id = s.setup_id AND o.final;

REVOKE ALL ON FUNCTION public.forward_outcome_report(BIGINT, BIGINT, TEXT[], TEXT[], INTEGER[], INTEGER[], TEXT[])
  FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.forward_nontrade_report(BIGINT, BIGINT, TEXT[], TEXT[], INTEGER[], INTEGER[], TEXT[])
  FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.paper_equity_series(BIGINT, BIGINT) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.forward_outcome_report(BIGINT, BIGINT, TEXT[], TEXT[], INTEGER[], INTEGER[], TEXT[])
  TO authenticated;
GRANT EXECUTE ON FUNCTION public.forward_nontrade_report(BIGINT, BIGINT, TEXT[], TEXT[], INTEGER[], INTEGER[], TEXT[])
  TO authenticated;
GRANT EXECUTE ON FUNCTION public.paper_equity_series(BIGINT, BIGINT) TO authenticated;
REVOKE ALL ON public.forward_outcome_log FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.forward_outcome_log TO authenticated;

-- Open trades: entered setups (status T) that have no final outcome yet.
CREATE VIEW public.forward_open_setups WITH (security_invoker = true) AS
  SELECT s.user_id, s.setup_id, s.strategy_id, s.version, s.symbol, s.side, s.horizon_min, s.rr, s.entry_ms
  FROM public.forward_setups s
  WHERE s.status = 'T' AND NOT EXISTS (
    SELECT 1 FROM public.forward_outcomes o
    WHERE o.user_id = s.user_id AND o.setup_id = s.setup_id AND o.final);
REVOKE ALL ON public.forward_open_setups FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.forward_open_setups TO authenticated;
