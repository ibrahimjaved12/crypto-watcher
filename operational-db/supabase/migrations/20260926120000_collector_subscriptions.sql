-- Derived collector input. The application/orchestration side owns user
-- watchlists; it assigns the shared, supported symbol set here and the collector
-- worker reads it. This is collector input/state, never a second watchlist
-- authority, and the worker holds no Lovable credentials.

CREATE TABLE public.collector_subscriptions (
  universe_name TEXT PRIMARY KEY CHECK (universe_name = 'binance-usdm-shared'),
  symbols TEXT[] NOT NULL,
  assigned_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE public.collector_subscriptions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.collector_subscriptions FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.collector_subscriptions TO service_role;

CREATE FUNCTION public.assign_collector_subscriptions(p_symbols TEXT[])
RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  normalized TEXT[];
BEGIN
  IF p_symbols IS NULL THEN
    RAISE EXCEPTION 'Invalid collector subscription set';
  END IF;
  SELECT coalesce(
      array_agg(DISTINCT upper(btrim(value)) ORDER BY upper(btrim(value))),
      ARRAY[]::TEXT[]
    )
    INTO normalized
  FROM unnest(p_symbols) AS value
  WHERE btrim(value) <> '';
  IF array_length(normalized, 1) > 100 THEN
    RAISE EXCEPTION 'Collector subscription set exceeds the supported maximum';
  END IF;
  INSERT INTO public.collector_subscriptions(universe_name, symbols, assigned_at)
    VALUES ('binance-usdm-shared', normalized, clock_timestamp())
  ON CONFLICT (universe_name) DO UPDATE SET
    symbols = EXCLUDED.symbols,
    assigned_at = clock_timestamp();
END;
$$;

CREATE FUNCTION public.get_collector_subscriptions()
RETURNS TEXT[]
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  SELECT coalesce(
    (SELECT symbols FROM public.collector_subscriptions
      WHERE universe_name = 'binance-usdm-shared'),
    ARRAY[]::TEXT[]
  );
$$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;
