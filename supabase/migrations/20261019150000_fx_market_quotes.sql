-- Published forex quotes (#433, part of #374). Same path as the crypto market_quotes (#323): the owner's
-- machine computes one row per subscribed symbol from the operational database's completed 1m BID bars
-- and the broker's symbol spec (#432), and upserts it here; the Worker only reads. Unlike market_quotes,
-- forex quotes are readable only by users entitled to forex (has_market('forex')); anon gets nothing.
-- A quote carries the bid (the bar close), the ask (bid + spread_points * the broker's point) and the
-- spread in MT5 points.
CREATE TABLE public.fx_market_quotes (
  symbol TEXT PRIMARY KEY CHECK (symbol ~ '^[A-Z0-9._-]{2,20}$'),
  instrument_id TEXT NOT NULL CHECK (
    instrument_id ~ '^(fx|fut):[A-Z0-9._-]{2,20}$' AND split_part(instrument_id, ':', 2) = symbol
  ),
  venue TEXT NOT NULL CHECK (venue ~ '^[a-z0-9-]{3,40}$'),
  bid DOUBLE PRECISION NOT NULL CHECK (bid > 0 AND bid < 'Infinity'::float8),
  ask DOUBLE PRECISION NOT NULL CHECK (ask >= bid AND ask < 'Infinity'::float8),
  spread_points INTEGER NOT NULL CHECK (spread_points >= 0),
  digits SMALLINT NOT NULL CHECK (digits BETWEEN 0 AND 10),
  -- Minutes -> percent change of the bid; keys '5', '15', '60', '240', '1440'; null when a window cannot
  -- be computed (including across a market closure).
  changes JSONB NOT NULL CHECK (
    jsonb_typeof(changes) = 'object' AND changes - ARRAY['5', '15', '60', '240', '1440'] = '{}'::jsonb
  ),
  -- 24h of 15m bid closes: [{"time": <open time ms>, "close": <bid>}, ...], oldest first, at most 96.
  chart JSONB NOT NULL CHECK (jsonb_typeof(chart) = 'array' AND jsonb_array_length(chart) <= 96),
  -- Open time of the newest completed 1m bar the quote was computed from.
  last_bar_at TIMESTAMPTZ NOT NULL,
  published_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.fx_market_quotes ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.fx_market_quotes FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.fx_market_quotes TO authenticated;
GRANT ALL ON public.fx_market_quotes TO service_role;

CREATE POLICY fx_market_quotes_entitled ON public.fx_market_quotes FOR SELECT TO authenticated
  USING (public.has_market('forex'));
-- No INSERT, UPDATE or DELETE policies and no grants: only service_role (which bypasses RLS) writes.

-- One batched upsert, forward-only like publish_market_quotes: an older or equal last_bar_at (a
-- restarted or duplicate publisher) leaves the stored row untouched, so retries and replays are
-- idempotent. Returns the number of rows written.
CREATE FUNCTION public.publish_fx_market_quotes(p_quotes JSONB)
RETURNS INTEGER
LANGUAGE plpgsql SET search_path = public
AS $$
DECLARE
  written INTEGER;
BEGIN
  IF p_quotes IS NULL OR jsonb_typeof(p_quotes) <> 'array' THEN
    RAISE EXCEPTION 'Invalid fx market quote batch';
  END IF;

  WITH incoming AS (
    SELECT q.symbol, q.instrument_id, q.venue, q.bid, q.ask, q.spread_points, q.digits, q.changes,
      q.chart, q.last_bar_at
    FROM jsonb_to_recordset(p_quotes) AS q(
      symbol TEXT, instrument_id TEXT, venue TEXT, bid DOUBLE PRECISION, ask DOUBLE PRECISION,
      spread_points INTEGER, digits SMALLINT, changes JSONB, chart JSONB, last_bar_at TIMESTAMPTZ
    )
  ), upserted AS (
    INSERT INTO public.fx_market_quotes AS m
      (symbol, instrument_id, venue, bid, ask, spread_points, digits, changes, chart, last_bar_at,
       published_at)
    SELECT symbol, instrument_id, venue, bid, ask, spread_points, digits, changes, chart, last_bar_at,
      now()
    FROM incoming
    ON CONFLICT (symbol) DO UPDATE SET
      instrument_id = EXCLUDED.instrument_id,
      venue = EXCLUDED.venue,
      bid = EXCLUDED.bid,
      ask = EXCLUDED.ask,
      spread_points = EXCLUDED.spread_points,
      digits = EXCLUDED.digits,
      changes = EXCLUDED.changes,
      chart = EXCLUDED.chart,
      last_bar_at = EXCLUDED.last_bar_at,
      published_at = now()
    WHERE EXCLUDED.last_bar_at > m.last_bar_at
    RETURNING 1
  )
  SELECT count(*)::INTEGER INTO written FROM upserted;

  RETURN written;
END;
$$;

REVOKE ALL ON FUNCTION public.publish_fx_market_quotes(JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.publish_fx_market_quotes(JSONB) TO service_role;
