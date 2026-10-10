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
