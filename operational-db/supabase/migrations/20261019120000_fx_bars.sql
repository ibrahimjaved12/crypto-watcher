-- Forex 1-minute BID bars from the MT5 gateway (#395, part of #374). Operational database only.
--
-- The forex collector (#394) writes COMPLETED one-minute bars here; the forex runner (#397) reads
-- them. The row contract is python/market_analysis/fx/bars.py (fx_bars_from_rows): UTC minute
-- open times, BID OHLC, the spread in MT5 POINTS and the tick volume. Forex has no real volume, no
-- quote volume and no source event time. Every object is modelled on its crypto twin in the
-- baseline (collector_recent_candles, collector_candle_conflicts, collector_health,
-- collector_subscriptions, collector_leases); no crypto object is touched. A missing minute is
-- not decided here: whether it is expected (market closed) or lost needs the trading calendar.

CREATE TABLE public.fx_recent_bars (
  venue TEXT NOT NULL CHECK (venue ~ '^[a-z0-9-]{3,40}$'),
  instrument_id TEXT NOT NULL CHECK (instrument_id ~ '^(fx|fut):[A-Z0-9._-]{2,20}$'),
  symbol TEXT NOT NULL,
  native_symbol TEXT NOT NULL CHECK (length(native_symbol) BETWEEN 1 AND 40),
  open_time TIMESTAMPTZ NOT NULL,
  open DOUBLE PRECISION NOT NULL CHECK (open > 0 AND open < 'Infinity'::float8),
  high DOUBLE PRECISION NOT NULL CHECK (high > 0 AND high < 'Infinity'::float8),
  low DOUBLE PRECISION NOT NULL CHECK (low > 0 AND low < 'Infinity'::float8),
  close DOUBLE PRECISION NOT NULL CHECK (close > 0 AND close < 'Infinity'::float8),
  -- ask - bid in MT5 points (SYMBOL_POINT = 10**-digits), not in tick sizes.
  spread_points INTEGER NOT NULL CHECK (spread_points >= 0),
  tick_volume BIGINT NOT NULL CHECK (tick_volume >= 0),
  transport TEXT NOT NULL CHECK (transport IN ('poll', 'history', 'stream')),
  received_at TIMESTAMPTZ NOT NULL,
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  -- One minute per row; the close time is always open_time + 1 minute, so it is not stored.
  PRIMARY KEY (venue, instrument_id, open_time),
  CHECK (split_part(instrument_id, ':', 2) = symbol),
  CHECK (high >= greatest(open, close) AND low <= least(open, close)),
  CHECK (mod(extract(epoch FROM open_time)::BIGINT, 60) = 0)
);
CREATE INDEX fx_recent_bars_time ON public.fx_recent_bars(open_time DESC);

-- Append-only conflict evidence (twin of collector_candle_conflicts). A stored bar is immutable: a
-- later offer with other values for the same minute is never inserted and never fails its batch;
-- it is appended here once per distinct offer. Conflicts are evidence, never a data source, and
-- never degrade collector health. Nothing reconciles forex bars yet, so there is no revisions table.
CREATE TABLE public.fx_bar_conflicts (
  conflict_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  venue TEXT NOT NULL,
  instrument_id TEXT NOT NULL,
  open_time TIMESTAMPTZ NOT NULL,
  stored JSONB NOT NULL,
  stored_transport TEXT NOT NULL CHECK (stored_transport IN ('poll', 'history', 'stream')),
  stored_hash TEXT NOT NULL,
  offered JSONB NOT NULL,
  offered_transport TEXT NOT NULL CHECK (offered_transport IN ('poll', 'history', 'stream')),
  offered_hash TEXT NOT NULL,
  differing_fields TEXT[] NOT NULL CHECK (
    cardinality(differing_fields) >= 1
    AND differing_fields <@ ARRAY['open', 'high', 'low', 'close', 'spread_points', 'tick_volume']
  ),
  detected_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (venue, instrument_id, open_time, offered_hash)
);
CREATE INDEX fx_bar_conflicts_recent ON public.fx_bar_conflicts(instrument_id, detected_at DESC);

CREATE TRIGGER fx_bar_conflicts_append_only
  BEFORE UPDATE OR DELETE ON public.fx_bar_conflicts
  FOR EACH ROW EXECUTE FUNCTION public.reject_candle_evidence_mutation();

-- A closed market is MARKET_CLOSED, never STALE. LOGIN_FAILED is how an expired demo account or a
-- wrong password shows. Conflicts only feed conflict_count_24h/last_conflict_at.
CREATE TABLE public.fx_collector_health (
  venue TEXT NOT NULL,
  instrument_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'LIVE', 'RECOVERING', 'STALE', 'UNAVAILABLE', 'MARKET_CLOSED', 'LOGIN_FAILED'
  )),
  last_bar_open_time TIMESTAMPTZ,
  lag_ms BIGINT CHECK (lag_ms IS NULL OR lag_ms >= 0),
  error_message TEXT,
  conflict_count_24h INTEGER NOT NULL DEFAULT 0 CHECK (conflict_count_24h >= 0),
  last_conflict_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (venue, instrument_id)
);

-- Derived collector input (twin of collector_subscriptions): the instrument set the application
-- assigns to the forex collector, as an array of {venue, instrument_id, native_symbol}.
CREATE TABLE public.fx_subscriptions (
  universe_name TEXT PRIMARY KEY CHECK (universe_name = 'fx-shared'),
  instruments JSONB NOT NULL CHECK (jsonb_typeof(instruments) = 'array'),
  assigned_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE public.fx_collector_leases (
  lease_name TEXT PRIMARY KEY CHECK (lease_name = 'fx-mt5-gateway'),
  instance_id UUID NOT NULL,
  leased_until TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE public.fx_recent_bars ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fx_bar_conflicts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fx_collector_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fx_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fx_collector_leases ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.fx_recent_bars, public.fx_bar_conflicts, public.fx_collector_health,
  public.fx_subscriptions, public.fx_collector_leases FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.fx_recent_bars, public.fx_bar_conflicts, public.fx_collector_health,
  public.fx_subscriptions, public.fx_collector_leases TO service_role;

-- Version identity of one bar's values ("fxbar-v1"), same text rule as collector_candle_hash:
-- float8 text is PostgreSQL's shortest exact form.
CREATE FUNCTION public.fx_bar_hash(
  p_venue TEXT, p_instrument_id TEXT, p_open_time TIMESTAMPTZ, p_open DOUBLE PRECISION,
  p_high DOUBLE PRECISION, p_low DOUBLE PRECISION, p_close DOUBLE PRECISION,
  p_spread_points INTEGER, p_tick_volume BIGINT
) RETURNS TEXT
LANGUAGE sql STABLE SET search_path = public
AS $$
  SELECT md5(concat_ws('|', 'fxbar-v1', p_venue, p_instrument_id,
    (extract(epoch FROM p_open_time) * 1000)::BIGINT::TEXT,
    p_open::TEXT, p_high::TEXT, p_low::TEXT, p_close::TEXT,
    p_spread_points::TEXT, p_tick_volume::TEXT));
$$;

-- Rolling 24-hour conflict count and latest conflict per fx health row; never changes status.
CREATE FUNCTION public.refresh_fx_conflict_counters()
RETURNS VOID
LANGUAGE sql SECURITY INVOKER SET search_path = public
AS $$
  UPDATE public.fx_collector_health h SET
    conflict_count_24h = coalesce(s.recent, 0),
    last_conflict_at = s.last_at
  FROM public.fx_collector_health base
  LEFT JOIN (
    SELECT venue, instrument_id,
      (count(*) FILTER (WHERE detected_at > clock_timestamp() - interval '24 hours'))::INTEGER
        AS recent,
      max(detected_at) AS last_at
    FROM public.fx_bar_conflicts
    GROUP BY venue, instrument_id
  ) s USING (venue, instrument_id)
  WHERE h.venue = base.venue AND h.instrument_id = base.instrument_id
    AND (h.conflict_count_24h, h.last_conflict_at)
      IS DISTINCT FROM (coalesce(s.recent, 0), s.last_at);
$$;

-- Completed bars only (the collector never sends the forming minute). Conflict policy as
-- record_collector_candles: a bar whose minute is already stored with different values is never
-- inserted and never fails the batch; it is appended to fx_bar_conflicts once per distinct offer.
-- Retention: bars older than p_retention_days (default 140, bounded 31..200) are deleted unless
-- they are among the newest 2000 bars of their series.
CREATE FUNCTION public.record_fx_bars(p_rows JSONB, p_retention_days INTEGER DEFAULT 140)
RETURNS TABLE (bar_identity TEXT)
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  old_before TIMESTAMPTZ;
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' OR jsonb_array_length(p_rows) < 1
     OR jsonb_array_length(p_rows) > 1000
     OR p_retention_days IS NULL OR p_retention_days < 31 OR p_retention_days > 200 THEN
    RAISE EXCEPTION 'Invalid fx bar batch';
  END IF;

  RETURN QUERY
  WITH incoming AS (
    SELECT x.*, to_jsonb(x) AS incoming_json
    FROM jsonb_to_recordset(p_rows) AS x(
      venue TEXT, instrument_id TEXT, symbol TEXT, native_symbol TEXT, open_time TIMESTAMPTZ,
      open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION, close DOUBLE PRECISION,
      spread_points INTEGER, tick_volume BIGINT, transport TEXT, received_at TIMESTAMPTZ
    )
  ),
  conflicting AS (
    SELECT i.*, to_jsonb(b) - 'inserted_at' AS stored_json, b.transport AS stored_transport,
      public.fx_bar_hash(b.venue, b.instrument_id, b.open_time, b.open, b.high, b.low, b.close,
        b.spread_points, b.tick_volume) AS stored_hash,
      public.fx_bar_hash(i.venue, i.instrument_id, i.open_time, i.open, i.high, i.low, i.close,
        i.spread_points, i.tick_volume) AS offered_hash,
      array_remove(ARRAY[
        CASE WHEN b.open <> i.open THEN 'open' END,
        CASE WHEN b.high <> i.high THEN 'high' END,
        CASE WHEN b.low <> i.low THEN 'low' END,
        CASE WHEN b.close <> i.close THEN 'close' END,
        CASE WHEN b.spread_points <> i.spread_points THEN 'spread_points' END,
        CASE WHEN b.tick_volume <> i.tick_volume THEN 'tick_volume' END
      ], NULL) AS differing_fields
    FROM incoming i
    JOIN public.fx_recent_bars b USING (venue, instrument_id, open_time)
    WHERE b.open <> i.open OR b.high <> i.high OR b.low <> i.low OR b.close <> i.close
      OR b.spread_points <> i.spread_points OR b.tick_volume <> i.tick_volume
  ),
  recorded AS (
    INSERT INTO public.fx_bar_conflicts (
      venue, instrument_id, open_time, stored, stored_transport, stored_hash,
      offered, offered_transport, offered_hash, differing_fields
    )
    SELECT DISTINCT ON (k.venue, k.instrument_id, k.open_time, k.offered_hash)
      k.venue, k.instrument_id, k.open_time, k.stored_json, k.stored_transport, k.stored_hash,
      k.incoming_json - 'received_at', k.transport, k.offered_hash, k.differing_fields
    FROM conflicting k
    ON CONFLICT DO NOTHING
    RETURNING 1
  ),
  inserted AS (
    INSERT INTO public.fx_recent_bars (
      venue, instrument_id, symbol, native_symbol, open_time, open, high, low, close,
      spread_points, tick_volume, transport, received_at
    )
    SELECT i.venue, i.instrument_id, i.symbol, i.native_symbol, i.open_time, i.open, i.high,
      i.low, i.close, i.spread_points, i.tick_volume, i.transport, i.received_at
    FROM incoming i
    WHERE NOT EXISTS (
      SELECT 1 FROM conflicting k
      WHERE k.venue = i.venue AND k.instrument_id = i.instrument_id AND k.open_time = i.open_time
    )
    ON CONFLICT (venue, instrument_id, open_time) DO NOTHING
    RETURNING venue, instrument_id, open_time
  )
  SELECT inserted.venue || '|' || inserted.instrument_id || '|'
    || (extract(epoch FROM inserted.open_time) * 1000)::BIGINT::TEXT
  FROM inserted;

  -- Conflict counters only; the measured health status is never changed by a conflict.
  PERFORM public.refresh_fx_conflict_counters();

  -- A bar is old once its minute ended before the retention cutoff; the newest 2000 bars of each
  -- series are always kept (runner warm-up across a long market closure). Same rule as the crypto
  -- retention, but the floor (the 2000th newest open time) is looked up once per series that has
  -- old bars instead of once per old bar, so a series held by the floor stays cheap every minute.
  old_before := clock_timestamp() - make_interval(days => p_retention_days) - interval '1 minute';
  WITH floors AS (
    SELECT s.venue, s.instrument_id, (
        SELECT k.open_time FROM public.fx_recent_bars k
        WHERE k.venue = s.venue AND k.instrument_id = s.instrument_id
        ORDER BY k.open_time DESC
        OFFSET 1999 LIMIT 1
      ) AS floor_time
    FROM (
      SELECT DISTINCT venue, instrument_id FROM public.fx_recent_bars WHERE open_time < old_before
    ) s
  )
  DELETE FROM public.fx_recent_bars b
    USING floors f
    WHERE b.venue = f.venue AND b.instrument_id = f.instrument_id
      AND b.open_time < old_before AND b.open_time < f.floor_time;
END;
$$;

-- Upsert one health row per (venue, instrument_id). Items: {venue, instrument_id, status,
-- last_bar_open_time (ISO or null), lag_ms, error_message}; 1 to 100 items, keys unique.
CREATE FUNCTION public.record_fx_collector_health(p_rows JSONB)
RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' OR jsonb_array_length(p_rows) < 1
     OR jsonb_array_length(p_rows) > 100
     OR EXISTS (
       SELECT 1 FROM jsonb_array_elements(p_rows) AS item(value)
       WHERE jsonb_typeof(item.value) <> 'object'
         OR coalesce(item.value->>'status', '') NOT IN (
           'LIVE', 'RECOVERING', 'STALE', 'UNAVAILABLE', 'MARKET_CLOSED', 'LOGIN_FAILED')
         OR coalesce(item.value->>'venue', '') !~ '^[a-z0-9-]{3,40}$'
         OR coalesce(item.value->>'instrument_id', '') !~ '^(fx|fut):[A-Z0-9._-]{2,20}$'
     )
     OR (SELECT count(DISTINCT (value->>'venue') || '|' || (value->>'instrument_id'))
         FROM jsonb_array_elements(p_rows)) <> jsonb_array_length(p_rows) THEN
    RAISE EXCEPTION 'Invalid fx collector health';
  END IF;

  INSERT INTO public.fx_collector_health (
    venue, instrument_id, status, last_bar_open_time, lag_ms, error_message
  )
  SELECT x.venue, x.instrument_id, x.status, x.last_bar_open_time, x.lag_ms,
    nullif(left(coalesce(x.error_message, ''), 1000), '')
  FROM jsonb_to_recordset(p_rows) AS x(
    venue TEXT, instrument_id TEXT, status TEXT, last_bar_open_time TIMESTAMPTZ,
    lag_ms BIGINT, error_message TEXT
  )
  ON CONFLICT (venue, instrument_id) DO UPDATE SET
    status = EXCLUDED.status,
    last_bar_open_time = EXCLUDED.last_bar_open_time,
    lag_ms = EXCLUDED.lag_ms,
    error_message = EXCLUDED.error_message,
    updated_at = clock_timestamp();
  -- The 24-hour conflict window decays with time, so every health write refreshes it.
  PERFORM public.refresh_fx_conflict_counters();
END;
$$;

CREATE FUNCTION public.get_fx_collector_health()
RETURNS JSONB
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  SELECT coalesce(jsonb_agg(jsonb_build_object(
      'venue', h.venue,
      'instrument_id', h.instrument_id,
      'status', h.status,
      'last_bar_open_time_ms', (extract(epoch FROM h.last_bar_open_time) * 1000)::BIGINT,
      'lag_ms', h.lag_ms,
      'error_message', h.error_message,
      'conflict_count_24h', h.conflict_count_24h,
      'last_conflict_at_ms', (extract(epoch FROM h.last_conflict_at) * 1000)::BIGINT,
      'updated_at_ms', (extract(epoch FROM h.updated_at) * 1000)::BIGINT
    ) ORDER BY h.venue, h.instrument_id), '[]'::jsonb)
  FROM public.fx_collector_health h;
$$;

-- Replace the shared forex instrument set (at most 100). Health rows of instruments no longer
-- assigned become UNAVAILABLE, as assign_collector_subscriptions does for crypto.
CREATE FUNCTION public.assign_fx_subscriptions(p_instruments JSONB)
RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  normalized JSONB;
BEGIN
  IF p_instruments IS NULL OR jsonb_typeof(p_instruments) <> 'array'
     OR jsonb_array_length(p_instruments) > 100
     OR EXISTS (
       SELECT 1 FROM jsonb_array_elements(p_instruments) AS item(value)
       WHERE jsonb_typeof(item.value) <> 'object'
         OR jsonb_typeof(item.value->'venue') IS DISTINCT FROM 'string'
         OR jsonb_typeof(item.value->'instrument_id') IS DISTINCT FROM 'string'
         OR jsonb_typeof(item.value->'native_symbol') IS DISTINCT FROM 'string'
         OR item.value->>'venue' !~ '^[a-z0-9-]{3,40}$'
         OR item.value->>'instrument_id' !~ '^(fx|fut):[A-Z0-9._-]{2,20}$'
         OR length(item.value->>'native_symbol') NOT BETWEEN 1 AND 40
     ) THEN
    RAISE EXCEPTION 'Invalid fx subscription set';
  END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_object(
      'venue', d.venue, 'instrument_id', d.instrument_id, 'native_symbol', d.native_symbol
    ) ORDER BY d.venue, d.instrument_id), '[]'::jsonb)
    INTO normalized
  FROM (
    SELECT DISTINCT ON (value->>'venue', value->>'instrument_id')
      value->>'venue' AS venue, value->>'instrument_id' AS instrument_id,
      value->>'native_symbol' AS native_symbol
    FROM jsonb_array_elements(p_instruments)
    ORDER BY value->>'venue', value->>'instrument_id'
  ) d;
  INSERT INTO public.fx_subscriptions(universe_name, instruments, assigned_at)
    VALUES ('fx-shared', normalized, clock_timestamp())
  ON CONFLICT (universe_name) DO UPDATE SET
    instruments = EXCLUDED.instruments,
    assigned_at = clock_timestamp();

  UPDATE public.fx_collector_health h SET
    status = 'UNAVAILABLE',
    error_message = 'collection disabled/unsubscribed',
    lag_ms = NULL,
    updated_at = clock_timestamp()
  WHERE NOT EXISTS (
    SELECT 1 FROM jsonb_array_elements(normalized) AS item(value)
    WHERE item.value->>'venue' = h.venue AND item.value->>'instrument_id' = h.instrument_id
  );
END;
$$;

CREATE FUNCTION public.get_fx_subscriptions()
RETURNS JSONB
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  SELECT coalesce(
    (SELECT instruments FROM public.fx_subscriptions WHERE universe_name = 'fx-shared'),
    '[]'::jsonb
  );
$$;

CREATE FUNCTION public.claim_fx_collector_lease(p_instance_id UUID, p_lease_seconds INTEGER DEFAULT 60)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_instance_id IS NULL OR p_lease_seconds < 30 OR p_lease_seconds > 300 THEN
    RAISE EXCEPTION 'Invalid fx collector lease';
  END IF;
  INSERT INTO public.fx_collector_leases(lease_name, instance_id, leased_until)
    VALUES ('fx-mt5-gateway', p_instance_id,
      clock_timestamp() + make_interval(secs => p_lease_seconds))
  ON CONFLICT (lease_name) DO UPDATE SET
    instance_id = EXCLUDED.instance_id,
    leased_until = EXCLUDED.leased_until,
    updated_at = clock_timestamp()
  WHERE public.fx_collector_leases.leased_until < clock_timestamp()
     OR public.fx_collector_leases.instance_id = EXCLUDED.instance_id;
  RETURN FOUND;
END;
$$;

CREATE FUNCTION public.renew_fx_collector_lease(p_instance_id UUID, p_lease_seconds INTEGER DEFAULT 60)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  IF p_instance_id IS NULL OR p_lease_seconds < 30 OR p_lease_seconds > 300 THEN
    RAISE EXCEPTION 'Invalid fx collector lease';
  END IF;
  UPDATE public.fx_collector_leases SET
    leased_until = clock_timestamp() + make_interval(secs => p_lease_seconds),
    updated_at = clock_timestamp()
  WHERE lease_name = 'fx-mt5-gateway'
    AND instance_id = p_instance_id AND leased_until >= clock_timestamp();
  RETURN FOUND;
END;
$$;

CREATE FUNCTION public.release_fx_collector_lease(p_instance_id UUID)
RETURNS VOID
LANGUAGE sql SECURITY INVOKER SET search_path = public
AS $$
  DELETE FROM public.fx_collector_leases
  WHERE lease_name = 'fx-mt5-gateway' AND instance_id = p_instance_id;
$$;

-- The newest p_limit bars of one series with open_time >= p_from_ms (all when NULL), oldest
-- first. Each element is exactly the gateway row shape fx_bars_from_rows accepts.
CREATE FUNCTION public.get_fx_bars(
  p_venue TEXT, p_instrument_id TEXT, p_from_ms BIGINT DEFAULT NULL, p_limit INTEGER DEFAULT 1000
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  rows JSONB;
BEGIN
  IF p_venue IS NULL OR p_instrument_id IS NULL OR p_limit IS NULL OR p_limit < 1
     OR p_limit > 20000 THEN
    RAISE EXCEPTION 'Invalid fx bar request';
  END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_object(
      'time_basis', 'utc',
      'venue', newest.venue,
      'instrument_id', newest.instrument_id,
      'symbol', newest.symbol,
      'native_symbol', newest.native_symbol,
      'transport', newest.transport,
      'open_time_ms', (extract(epoch FROM newest.open_time) * 1000)::BIGINT,
      'open', newest.open,
      'high', newest.high,
      'low', newest.low,
      'close', newest.close,
      'spread_points', newest.spread_points,
      'tick_volume', newest.tick_volume,
      'received_at_ms', (extract(epoch FROM newest.received_at) * 1000)::BIGINT
    ) ORDER BY newest.open_time ASC), '[]'::jsonb)
    INTO rows
  FROM (
    SELECT b.*
    FROM public.fx_recent_bars b
    WHERE b.venue = p_venue AND b.instrument_id = p_instrument_id
      AND (p_from_ms IS NULL OR b.open_time >= timestamptz 'epoch' + p_from_ms * interval '1 millisecond')
    ORDER BY b.open_time DESC
    LIMIT p_limit
  ) newest;
  RETURN rows;
END;
$$;

-- Stored range per series. Prerequisite checks only: what is missing needs the trading calendar.
CREATE FUNCTION public.get_fx_bar_coverage()
RETURNS JSONB
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  SELECT coalesce(jsonb_agg(jsonb_build_object(
      'venue', s.venue,
      'instrument_id', s.instrument_id,
      'bar_count', s.bar_count,
      'oldest_open_time_ms', (extract(epoch FROM s.oldest) * 1000)::BIGINT,
      'newest_open_time_ms', (extract(epoch FROM s.newest) * 1000)::BIGINT
    ) ORDER BY s.venue, s.instrument_id), '[]'::jsonb)
  FROM (
    SELECT venue, instrument_id, count(*) AS bar_count, min(open_time) AS oldest,
      max(open_time) AS newest
    FROM public.fx_recent_bars
    GROUP BY venue, instrument_id
  ) s;
$$;

REVOKE ALL ON FUNCTION
  public.fx_bar_hash(TEXT, TEXT, TIMESTAMPTZ, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, INTEGER, BIGINT),
  public.refresh_fx_conflict_counters(),
  public.record_fx_bars(JSONB, INTEGER),
  public.record_fx_collector_health(JSONB),
  public.get_fx_collector_health(),
  public.assign_fx_subscriptions(JSONB),
  public.get_fx_subscriptions(),
  public.claim_fx_collector_lease(UUID, INTEGER),
  public.renew_fx_collector_lease(UUID, INTEGER),
  public.release_fx_collector_lease(UUID),
  public.get_fx_bars(TEXT, TEXT, BIGINT, INTEGER),
  public.get_fx_bar_coverage()
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION
  public.fx_bar_hash(TEXT, TEXT, TIMESTAMPTZ, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, INTEGER, BIGINT),
  public.refresh_fx_conflict_counters(),
  public.record_fx_bars(JSONB, INTEGER),
  public.record_fx_collector_health(JSONB),
  public.get_fx_collector_health(),
  public.assign_fx_subscriptions(JSONB),
  public.get_fx_subscriptions(),
  public.claim_fx_collector_lease(UUID, INTEGER),
  public.renew_fx_collector_lease(UUID, INTEGER),
  public.release_fx_collector_lease(UUID),
  public.get_fx_bars(TEXT, TEXT, BIGINT, INTEGER),
  public.get_fx_bar_coverage()
  TO service_role;
