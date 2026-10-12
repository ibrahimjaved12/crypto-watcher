-- Published market quotes (#323). The owner's machine computes one row per symbol from the collector's
-- completed candles and upserts it here; the Worker only reads. No exchange is called from the Worker.
-- Shared market data, no user data: readable by anon and authenticated, writable only by service_role.
CREATE TABLE public.market_quotes (
  symbol TEXT PRIMARY KEY CHECK (symbol <> '' AND symbol = upper(symbol)),
  source TEXT NOT NULL CHECK (source <> ''),
  price_type TEXT NOT NULL CHECK (price_type = 'trade'),
  price DOUBLE PRECISION NOT NULL CHECK (price > 0),
  -- Minutes -> percent change; keys '5', '15', '60', '240', '1440'; null when a window cannot be computed.
  changes JSONB NOT NULL CHECK (jsonb_typeof(changes) = 'object'),
  -- 24h of 15m closes: [{"time": <open time ms>, "close": <price>}, ...], oldest first.
  chart JSONB NOT NULL CHECK (jsonb_typeof(chart) = 'array'),
  -- Open time of the newest completed 1m candle the quote was computed from.
  last_candle_at TIMESTAMPTZ NOT NULL,
  published_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.market_quotes ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.market_quotes FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.market_quotes TO anon, authenticated;
GRANT ALL ON public.market_quotes TO service_role;

CREATE POLICY market_quotes_select_all ON public.market_quotes FOR SELECT TO anon, authenticated
  USING (true);
-- No INSERT, UPDATE or DELETE policies and no grants: only service_role (which bypasses RLS) writes.

-- One batched upsert. A row only moves forward: an older or equal last_candle_at (a restarted or
-- duplicate publisher) leaves the stored row untouched, so retries and replays are idempotent.
-- Returns the number of rows written.
CREATE FUNCTION public.publish_market_quotes(p_quotes JSONB)
RETURNS INTEGER
LANGUAGE plpgsql SET search_path = public
AS $$
DECLARE
  written INTEGER;
BEGIN
  IF p_quotes IS NULL OR jsonb_typeof(p_quotes) <> 'array' THEN
    RAISE EXCEPTION 'Invalid market quote batch';
  END IF;

  WITH incoming AS (
    SELECT q.symbol, q.source, q.price_type, q.price, q.changes, q.chart, q.last_candle_at
    FROM jsonb_to_recordset(p_quotes) AS q(
      symbol TEXT, source TEXT, price_type TEXT, price DOUBLE PRECISION,
      changes JSONB, chart JSONB, last_candle_at TIMESTAMPTZ
    )
  ), upserted AS (
    INSERT INTO public.market_quotes AS m
      (symbol, source, price_type, price, changes, chart, last_candle_at, published_at)
    SELECT symbol, source, price_type, price, changes, chart, last_candle_at, now() FROM incoming
    ON CONFLICT (symbol) DO UPDATE SET
      source = EXCLUDED.source,
      price_type = EXCLUDED.price_type,
      price = EXCLUDED.price,
      changes = EXCLUDED.changes,
      chart = EXCLUDED.chart,
      last_candle_at = EXCLUDED.last_candle_at,
      published_at = now()
    WHERE EXCLUDED.last_candle_at > m.last_candle_at
    RETURNING 1
  )
  SELECT count(*)::INTEGER INTO written FROM upserted;

  RETURN written;
END;
$$;

REVOKE ALL ON FUNCTION public.publish_market_quotes(JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.publish_market_quotes(JSONB) TO service_role;
