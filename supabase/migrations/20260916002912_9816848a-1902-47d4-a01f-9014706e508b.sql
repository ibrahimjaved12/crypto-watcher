CREATE TABLE public.ta_signals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  symbol text NOT NULL,
  timeframe integer NOT NULL CHECK (timeframe IN (15, 60, 240)),
  candle_at timestamptz NOT NULL,
  detected_at timestamptz NOT NULL DEFAULT now(),
  source text NOT NULL,
  version text NOT NULL,
  price double precision NOT NULL CHECK (price > 0),
  indicators jsonb NOT NULL,
  patterns text[] NOT NULL,
  outcome_status text NOT NULL DEFAULT 'pending' CHECK (outcome_status IN ('pending', 'measured', 'unavailable')),
  outcome_at timestamptz,
  outcome_price double precision,
  return_pct double precision,
  UNIQUE (user_id, symbol, timeframe, candle_at, version)
);
CREATE INDEX ta_signals_history ON public.ta_signals(user_id, candle_at DESC);
CREATE INDEX ta_signals_pending ON public.ta_signals(user_id, symbol, timeframe) WHERE outcome_status = 'pending';
ALTER TABLE public.ta_signals ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.ta_signals TO authenticated;
GRANT ALL ON public.ta_signals TO service_role;
CREATE POLICY "own TA signals" ON public.ta_signals FOR SELECT TO authenticated USING (auth.uid() = user_id);