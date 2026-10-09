-- Forward trend track (#239 P14, #222): the daily trend Mode B variants as hypothetical portfolios.
-- Account-scoped and append-only like the other forward tables (UPDATE/DELETE rejected by
-- public.reject_forward_mutation). Idempotent per (track, day): a re-run inserts nothing new.
-- Ledger and weight rows are written before the state row of their run, so a state never claims a
-- day whose ledger row is missing. Rows are written by the server (service role) and read by their
-- owner (RLS). Hypothetical track: no margin or liquidation model; no variant has a validated edge.

CREATE TABLE public.forward_trend_weights (
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  track TEXT NOT NULL CHECK (length(track) BETWEEN 1 AND 64),
  day_ms BIGINT NOT NULL CHECK (day_ms >= 0 AND day_ms % 86400000 = 0),
  decided_from_close_ms BIGINT NOT NULL CHECK (decided_from_close_ms = day_ms - 86400000),
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
  turnover_ppm BIGINT NOT NULL CHECK (turnover_ppm >= 0),
  gross_ppm BIGINT NOT NULL CHECK (gross_ppm >= 0),
  symbols_active INTEGER NOT NULL CHECK (symbols_active >= 0),
  weights JSONB NOT NULL CHECK (jsonb_typeof(weights) = 'object'),
  version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
  params_hash TEXT NOT NULL CHECK (params_hash ~ '^[0-9a-f]{64}$'),
  run_key TEXT NOT NULL CHECK (length(run_key) BETWEEN 1 AND 128),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id, track, day_ms)
);
CREATE INDEX forward_trend_ledger_days ON public.forward_trend_ledger(user_id, day_ms);

-- One row per run: the states after it (per track), what was finalised and why not.
CREATE TABLE public.forward_trend_state (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  run_key TEXT NOT NULL CHECK (length(run_key) BETWEEN 1 AND 128),
  trigger TEXT NOT NULL CHECK (trigger IN ('daily', 'on_demand')),
  status TEXT NOT NULL CHECK (status IN ('ok', 'partial')),
  reason TEXT CHECK (reason IS NULL OR length(reason) <= 2000),
  last_complete_day_ms BIGINT NOT NULL CHECK (last_complete_day_ms % 86400000 = 0),
  through_day_ms BIGINT CHECK (through_day_ms IS NULL OR through_day_ms % 86400000 = 0),
  states JSONB NOT NULL CHECK (jsonb_typeof(states) = 'object'),
  versions JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(versions) = 'object'),
  params_hash TEXT NOT NULL CHECK (params_hash ~ '^[0-9a-f]{64}$'),
  tracks JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(tracks) = 'array'),
  funding_unavailable JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(funding_unavailable) = 'array'),
  assumptions JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(assumptions) = 'array'),
  freshness JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(freshness) = 'object'),
  counts JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(counts) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (user_id, run_key)
);
CREATE INDEX forward_trend_state_latest ON public.forward_trend_state(user_id, created_at DESC);

DO $$
DECLARE
  name TEXT;
BEGIN
  FOREACH name IN ARRAY ARRAY['forward_trend_weights', 'forward_trend_ledger', 'forward_trend_state']
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

COMMENT ON TABLE public.forward_trend_ledger IS
  'Hypothetical daily trend portfolio track (Mode B, #239 P14): no margin or liquidation model; no validated edge.';
