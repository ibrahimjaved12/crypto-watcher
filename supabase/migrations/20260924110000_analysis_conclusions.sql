CREATE TABLE public.analysis_conclusions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
  supersedes_id UUID,

  instrument_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  provider TEXT NOT NULL CHECK (provider IN
    ('binance-usdm', 'kraken-futures', 'okx-usdt-swap')),
  source_instrument_id TEXT NOT NULL,
  source_native_symbol TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  market_type TEXT NOT NULL DEFAULT 'futures' CHECK (market_type = 'futures'),
  contract_type TEXT NOT NULL DEFAULT 'perpetual' CHECK (contract_type = 'perpetual'),

  timeframe_minutes INTEGER NOT NULL CHECK (timeframe_minutes > 0),
  direction TEXT NOT NULL CHECK (direction IN
    ('bullish', 'bearish', 'neutral', 'unavailable')),
  classification TEXT NOT NULL CHECK (length(classification) BETWEEN 1 AND 100),
  reference_price NUMERIC CHECK (reference_price > 0),
  price_type TEXT NOT NULL CHECK (price_type IN ('trade', 'mark', 'index')),

  source_event_at TIMESTAMPTZ,
  completed_candle_at TIMESTAMPTZ,
  detected_at TIMESTAMPTZ NOT NULL,
  evaluated_at TIMESTAMPTZ NOT NULL,
  persisted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  source_retrieved_at TIMESTAMPTZ,
  source_freshness_ms BIGINT CHECK (source_freshness_ms >= 0),

  status TEXT NOT NULL CHECK (status IN
    ('ok', 'stale', 'unavailable', 'insufficient_history', 'ambiguous')),
  score NUMERIC CHECK (score BETWEEN -100 AND 100),
  score_kind TEXT NOT NULL DEFAULT 'ranking' CHECK (score_kind = 'ranking'),
  factor_breakdown JSONB NOT NULL CHECK (jsonb_typeof(factor_breakdown) = 'object'),
  reasons JSONB NOT NULL CHECK (jsonb_typeof(reasons) = 'array'),

  ta_version TEXT NOT NULL CHECK (length(ta_version) BETWEEN 1 AND 100),
  strategy_version TEXT NOT NULL CHECK (length(strategy_version) BETWEEN 1 AND 100),
  model_version TEXT,
  schema_version INTEGER NOT NULL CHECK (schema_version > 0),
  configuration_version TEXT NOT NULL CHECK (length(configuration_version) BETWEEN 1 AND 100),
  input_hash TEXT,
  input_reference TEXT,

  CONSTRAINT analysis_conclusions_user_idempotency_key
    UNIQUE (user_id, idempotency_key),
  CONSTRAINT analysis_conclusions_user_id_id_key UNIQUE (user_id, id),
  CONSTRAINT analysis_conclusions_instrument_identity_fkey
    FOREIGN KEY (instrument_id, symbol)
    REFERENCES public.market_instruments(id, native_symbol),
  CONSTRAINT analysis_conclusions_supersedes_fkey
    FOREIGN KEY (user_id, supersedes_id)
    REFERENCES public.analysis_conclusions(user_id, id),
  CONSTRAINT analysis_conclusions_not_self_superseding
    CHECK (supersedes_id IS NULL OR supersedes_id <> id),
  CONSTRAINT analysis_conclusions_input_reference_check CHECK (
    (input_hash IS NULL OR input_hash ~ '^[0-9a-f]{64}$') AND
    (input_reference IS NOT NULL OR input_hash IS NOT NULL)
  ),
  CONSTRAINT analysis_conclusions_provider_provenance_check CHECK (
    price_type = 'trade' AND
    source_instrument_id = provider || ':' || source_native_symbol AND (
      (provider = 'binance-usdm' AND endpoint = '/fapi/v1/klines') OR
      (provider = 'kraken-futures' AND endpoint = '/api/charts/v1/trade/:symbol/:resolution') OR
      (provider = 'okx-usdt-swap' AND endpoint = '/api/v5/market/candles')
    )
  )
);

CREATE INDEX analysis_conclusions_history
  ON public.analysis_conclusions(user_id, evaluated_at DESC);
CREATE INDEX analysis_conclusions_instrument_history
  ON public.analysis_conclusions(user_id, instrument_id, timeframe_minutes, evaluated_at DESC);

CREATE FUNCTION public.reject_analysis_conclusion_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
  RAISE EXCEPTION 'analysis conclusions are immutable'
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER analysis_conclusions_immutable
  BEFORE UPDATE OR DELETE ON public.analysis_conclusions
  FOR EACH ROW EXECUTE FUNCTION public.reject_analysis_conclusion_mutation();

ALTER TABLE public.analysis_conclusions ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.analysis_conclusions TO authenticated;
GRANT SELECT, INSERT ON public.analysis_conclusions TO service_role;
CREATE POLICY "own analysis conclusions"
  ON public.analysis_conclusions FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

COMMENT ON TABLE public.analysis_conclusions IS
  'Append-only analysis/activity log; separate from legacy TA snapshots, outcomes, and simulation.';
