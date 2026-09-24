-- Scheduled TA now persists only results returned by the canonical Python package.
-- Pre-cutover snapshots are disposable development data and share the same ta-v2
-- label, so clear them before making the Python provenance fields mandatory.
DELETE FROM public.ta_signals;

ALTER TABLE public.ta_signals
  ADD COLUMN source_instrument_id TEXT NOT NULL,
  ADD COLUMN source_native_symbol TEXT NOT NULL,
  ADD COLUMN source_event_at TIMESTAMPTZ NOT NULL,
  ADD COLUMN evaluated_at TIMESTAMPTZ NOT NULL,
  ADD COLUMN strategy_version TEXT NOT NULL,
  ADD COLUMN classification TEXT NOT NULL,
  ADD COLUMN score DOUBLE PRECISION,
  ADD COLUMN atr_pct DOUBLE PRECISION,
  ADD COLUMN factor_breakdown JSONB NOT NULL,
  ADD COLUMN reasons TEXT[] NOT NULL,
  ADD CONSTRAINT ta_signals_source_instrument_check CHECK (
    source_instrument_id = source || ':' || source_native_symbol
  ),
  ADD CONSTRAINT ta_signals_python_versions_check CHECK (
    version = 'ta-v2' AND strategy_version = 'interpretation-v1'
  ),
  ADD CONSTRAINT ta_signals_classification_check CHECK (
    classification IN ('bullish', 'bearish', 'neutral', 'unavailable')
  ),
  ADD CONSTRAINT ta_signals_score_check CHECK (
    score IS NULL OR (score >= -100 AND score <= 100)
  ),
  ADD CONSTRAINT ta_signals_atr_pct_check CHECK (
    atr_pct IS NULL OR (atr_pct >= 0 AND atr_pct < 'Infinity'::float8)
  ),
  ADD CONSTRAINT ta_signals_factor_breakdown_check CHECK (
    jsonb_typeof(factor_breakdown) = 'object'
  ),
  ADD CONSTRAINT ta_signals_python_timestamps_check CHECK (
    source_event_at = candle_at + timeframe * interval '1 minute'
    AND detected_at >= source_event_at
    AND evaluated_at >= detected_at
  );

COMMENT ON TABLE public.ta_signals IS
  'Idempotent completed-candle TA snapshots calculated by the shared Python package and persisted by TanStack.';
COMMENT ON TABLE public.analysis_conclusions IS
  'Append-only analysis/activity log for domains outside scheduled completed-candle TA.';

CREATE FUNCTION public.reject_ta_conclusion_rewrite()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = public
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'TA conclusion and provenance are immutable'
      USING ERRCODE = '23514';
  END IF;
  IF ROW(
    NEW.user_id, NEW.symbol, NEW.instrument_id, NEW.source_instrument_id,
    NEW.source_native_symbol, NEW.timeframe, NEW.candle_at, NEW.source_event_at,
    NEW.evaluated_at, NEW.detected_at, NEW.source, NEW.endpoint, NEW.price_type,
    NEW.version, NEW.strategy_version, NEW.price, NEW.classification, NEW.score,
    NEW.atr_pct, NEW.factor_breakdown, NEW.reasons, NEW.indicators, NEW.patterns
  ) IS DISTINCT FROM ROW(
    OLD.user_id, OLD.symbol, OLD.instrument_id, OLD.source_instrument_id,
    OLD.source_native_symbol, OLD.timeframe, OLD.candle_at, OLD.source_event_at,
    OLD.evaluated_at, OLD.detected_at, OLD.source, OLD.endpoint, OLD.price_type,
    OLD.version, OLD.strategy_version, OLD.price, OLD.classification, OLD.score,
    OLD.atr_pct, OLD.factor_breakdown, OLD.reasons, OLD.indicators, OLD.patterns
  ) THEN
    RAISE EXCEPTION 'TA conclusion and provenance are immutable'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER ta_conclusion_immutable
  BEFORE UPDATE OR DELETE ON public.ta_signals
  FOR EACH ROW EXECUTE FUNCTION public.reject_ta_conclusion_rewrite();
