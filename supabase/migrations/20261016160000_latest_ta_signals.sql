-- Latest saved TA snapshot per watched pair for one timeframe, in ONE round trip (#30 current assessment).
-- SECURITY INVOKER: the caller's RLS on ta_signals ("own TA signals") still applies, so a user only ever
-- sees their own rows. A pair that has fallen behind the others still returns its own newest row.
CREATE INDEX IF NOT EXISTS ta_signals_latest
  ON public.ta_signals (user_id, timeframe, symbol, candle_at DESC);

CREATE FUNCTION public.latest_ta_signals(p_timeframe INTEGER, p_symbols TEXT[])
RETURNS TABLE (
  symbol TEXT, timeframe INTEGER, candle_at TIMESTAMPTZ, detected_at TIMESTAMPTZ, price DOUBLE PRECISION,
  classification TEXT, score DOUBLE PRECISION, atr_pct DOUBLE PRECISION, indicators JSONB, factor_breakdown JSONB,
  patterns TEXT[]
)
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = public
AS $$
#variable_conflict use_column
BEGIN
  -- Bounded by the watchlist size limit (MAX_WATCHLIST_SIZE = 10).
  IF p_symbols IS NULL OR cardinality(p_symbols) > 10 THEN
    RAISE EXCEPTION 'At most 10 symbols';
  END IF;
  RETURN QUERY
    SELECT DISTINCT ON (t.symbol) t.symbol, t.timeframe, t.candle_at, t.detected_at, t.price,
           t.classification, t.score, t.atr_pct, t.indicators, t.factor_breakdown, t.patterns
    FROM public.ta_signals t
    WHERE t.timeframe = p_timeframe AND t.symbol = ANY (p_symbols)
    ORDER BY t.symbol, t.candle_at DESC, t.detected_at DESC, t.id DESC;
END;
$$;

COMMENT ON FUNCTION public.latest_ta_signals(INTEGER, TEXT[]) IS
  'Newest ta_signals row per symbol for one timeframe (DISTINCT ON), at most 10 symbols, only the columns the dashboard reads. SECURITY INVOKER so RLS scopes it to the caller.';

REVOKE ALL ON FUNCTION public.latest_ta_signals(INTEGER, TEXT[]) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.latest_ta_signals(INTEGER, TEXT[]) TO authenticated, service_role;
