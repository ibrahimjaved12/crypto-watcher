-- Forex symbol contract specs (#432, part of #374). Operational database only.
--
-- Contract details (digits, point, tick, contract size, lot limits, currencies, swaps) come from the
-- broker and are never guessed (VenueSymbol in python/market_analysis/instruments.py, FxContract in
-- python/market_analysis/fx/costs.py). The bars store spread_points in MT5 points but not the point
-- size, so this table is what turns a bar into an ask, a cost or a lot size. Modelled on
-- fx_calendar_observations: append-only, versioned observations. The gateway writes a spec once per
-- change: a spec whose economic fields equal a stored one (same spec_hash) is not stored again.
-- MT5 reports tick_value in the ACCOUNT currency, so the account currency is stored with it.

CREATE TABLE public.fx_symbol_spec_observations (
  observation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  venue TEXT NOT NULL CHECK (venue ~ '^[a-z0-9-]{3,40}$'),
  instrument_id TEXT NOT NULL CHECK (
    instrument_id ~ '^(fx|fut):[A-Z0-9._-]{2,20}$' AND split_part(instrument_id, ':', 2) <> ''
  ),
  native_symbol TEXT NOT NULL CHECK (length(native_symbol) BETWEEN 1 AND 40),
  digits SMALLINT NOT NULL CHECK (digits BETWEEN 0 AND 10),
  point NUMERIC NOT NULL CHECK (point > 0),
  tick_size NUMERIC NOT NULL CHECK (tick_size > 0),
  tick_value NUMERIC NOT NULL CHECK (tick_value > 0),
  contract_size NUMERIC NOT NULL CHECK (contract_size > 0),
  volume_min NUMERIC NOT NULL CHECK (volume_min > 0),
  volume_max NUMERIC NOT NULL CHECK (volume_max > 0),
  volume_step NUMERIC NOT NULL CHECK (volume_step > 0),
  -- Not always ISO currencies (XAU, USOIL).
  base_currency TEXT NOT NULL CHECK (length(base_currency) BETWEEN 1 AND 10),
  profit_currency TEXT NOT NULL CHECK (length(profit_currency) BETWEEN 1 AND 10),
  margin_currency TEXT NOT NULL CHECK (length(margin_currency) BETWEEN 1 AND 10),
  -- The currency tick_value is expressed in.
  account_currency TEXT NOT NULL CHECK (account_currency ~ '^[A-Z]{3}$'),
  trade_mode SMALLINT NOT NULL CHECK (trade_mode >= 0),
  swap_mode SMALLINT NOT NULL CHECK (swap_mode >= 0),
  swap_long NUMERIC NOT NULL,
  swap_short NUMERIC NOT NULL,
  swap_rollover3days SMALLINT NOT NULL CHECK (swap_rollover3days BETWEEN 0 AND 6),
  spec_hash TEXT NOT NULL CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
  observed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (venue, instrument_id, spec_hash),
  CHECK (volume_min <= volume_max),
  CHECK (point = power(10::numeric, -digits))
);
CREATE INDEX fx_symbol_spec_observations_latest
  ON public.fx_symbol_spec_observations(venue, instrument_id, observation_id DESC);

CREATE TRIGGER fx_symbol_spec_observations_append_only
  BEFORE UPDATE OR DELETE ON public.fx_symbol_spec_observations
  FOR EACH ROW EXECUTE FUNCTION public.reject_candle_evidence_mutation();

ALTER TABLE public.fx_symbol_spec_observations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.fx_symbol_spec_observations FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.fx_symbol_spec_observations TO service_role;

-- Items: every column except observation_id, observed_at and spec_hash; numbers as JSON numbers;
-- no other key. 1 to 500 per call. The hash is computed here (sha256 of every economic field, not
-- native_symbol), never taken from the caller; numbers enter it with trailing zeros trimmed, so 1.0
-- and 1 are the same spec. Returns the number of new observations. Any invalid item rejects the whole
-- call with one generic message that never echoes a value. Note: ON CONFLICT DO NOTHING still draws
-- identity values, so observation ids are not contiguous.
CREATE FUNCTION public.record_fx_symbol_specs(p_rows JSONB)
RETURNS INTEGER
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  written INTEGER;
  keys CONSTANT TEXT[] := ARRAY[
    'account_currency', 'base_currency', 'contract_size', 'digits', 'instrument_id',
    'margin_currency', 'native_symbol', 'point', 'profit_currency', 'swap_long', 'swap_mode',
    'swap_rollover3days', 'swap_short', 'tick_size', 'tick_value', 'trade_mode', 'venue',
    'volume_max', 'volume_min', 'volume_step'
  ];
  text_keys CONSTANT TEXT[] := ARRAY[
    'account_currency', 'base_currency', 'instrument_id', 'margin_currency', 'native_symbol',
    'profit_currency', 'venue'
  ];
  integer_keys CONSTANT TEXT[] := ARRAY['digits', 'swap_mode', 'swap_rollover3days', 'trade_mode'];
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' THEN
    RAISE EXCEPTION 'Invalid fx symbol spec batch';
  END IF;
  IF jsonb_array_length(p_rows) < 1 OR jsonb_array_length(p_rows) > 500
     OR EXISTS (
       SELECT 1 FROM jsonb_array_elements(p_rows) AS item(value)
       WHERE jsonb_typeof(item.value) <> 'object'
     ) THEN
    RAISE EXCEPTION 'Invalid fx symbol spec batch';
  END IF;
  IF EXISTS (
       SELECT 1 FROM jsonb_array_elements(p_rows) AS item(value)
       -- Exact key set; byte order so the locale cannot reorder underscores; {} is caught too.
       WHERE coalesce((SELECT array_agg(k ORDER BY k COLLATE "C") FROM jsonb_object_keys(item.value) AS k),
                      ARRAY[]::TEXT[]) <> keys
         OR EXISTS (
           SELECT 1 FROM unnest(keys) AS k
           WHERE jsonb_typeof(item.value->k) IS DISTINCT FROM
             CASE WHEN k = ANY(text_keys) THEN 'string' ELSE 'number' END
         )
         OR EXISTS (
           SELECT 1 FROM unnest(integer_keys) AS k WHERE (item.value->>k) !~ '^-?[0-9]+$'
         )
         OR item.value->>'venue' !~ '^[a-z0-9-]{3,40}$'
         OR item.value->>'instrument_id' !~ '^(fx|fut):[A-Z0-9._-]{2,20}$'
         OR item.value->>'account_currency' !~ '^[A-Z]{3}$'
     ) THEN
    RAISE EXCEPTION 'Invalid fx symbol spec batch';
  END IF;

  BEGIN
    INSERT INTO public.fx_symbol_spec_observations (
      venue, instrument_id, native_symbol, digits, point, tick_size, tick_value, contract_size,
      volume_min, volume_max, volume_step, base_currency, profit_currency, margin_currency,
      account_currency, trade_mode, swap_mode, swap_long, swap_short, swap_rollover3days, spec_hash
    )
    SELECT s.venue, s.instrument_id, s.native_symbol, s.digits, s.point, s.tick_size, s.tick_value,
      s.contract_size, s.volume_min, s.volume_max, s.volume_step, s.base_currency, s.profit_currency,
      s.margin_currency, s.account_currency, s.trade_mode, s.swap_mode, s.swap_long, s.swap_short,
      s.swap_rollover3days,
      encode(sha256(convert_to(concat_ws('|', 'fxspec-v1', s.venue, s.instrument_id, s.digits::TEXT,
        trim_scale(s.point)::TEXT, trim_scale(s.tick_size)::TEXT, trim_scale(s.tick_value)::TEXT,
        trim_scale(s.contract_size)::TEXT, trim_scale(s.volume_min)::TEXT,
        trim_scale(s.volume_max)::TEXT, trim_scale(s.volume_step)::TEXT, s.base_currency,
        s.profit_currency, s.margin_currency, s.account_currency, s.trade_mode::TEXT,
        s.swap_mode::TEXT, trim_scale(s.swap_long)::TEXT, trim_scale(s.swap_short)::TEXT,
        s.swap_rollover3days::TEXT), 'UTF8')), 'hex')
    FROM (
      SELECT item.position,
        item.value->>'venue' AS venue, item.value->>'instrument_id' AS instrument_id,
        item.value->>'native_symbol' AS native_symbol, (item.value->>'digits')::SMALLINT AS digits,
        (item.value->>'point')::NUMERIC AS point, (item.value->>'tick_size')::NUMERIC AS tick_size,
        (item.value->>'tick_value')::NUMERIC AS tick_value,
        (item.value->>'contract_size')::NUMERIC AS contract_size,
        (item.value->>'volume_min')::NUMERIC AS volume_min,
        (item.value->>'volume_max')::NUMERIC AS volume_max,
        (item.value->>'volume_step')::NUMERIC AS volume_step,
        item.value->>'base_currency' AS base_currency,
        item.value->>'profit_currency' AS profit_currency,
        item.value->>'margin_currency' AS margin_currency,
        item.value->>'account_currency' AS account_currency,
        (item.value->>'trade_mode')::SMALLINT AS trade_mode,
        (item.value->>'swap_mode')::SMALLINT AS swap_mode,
        (item.value->>'swap_long')::NUMERIC AS swap_long,
        (item.value->>'swap_short')::NUMERIC AS swap_short,
        (item.value->>'swap_rollover3days')::SMALLINT AS swap_rollover3days
      FROM jsonb_array_elements(p_rows) WITH ORDINALITY AS item(value, position)
    ) s
    ORDER BY s.position
    ON CONFLICT (venue, instrument_id, spec_hash) DO NOTHING;
    GET DIAGNOSTICS written = ROW_COUNT;
  EXCEPTION
    -- A table CHECK (e.g. a point that is not 10^-digits) or an out-of-range SMALLINT. No SQLERRM:
    -- the message must never echo a value.
    WHEN check_violation OR not_null_violation OR numeric_value_out_of_range
      OR invalid_text_representation THEN
      RAISE EXCEPTION 'Invalid fx symbol spec batch';
  END;
  RETURN written;
END;
$$;

-- The latest observation per (venue, instrument_id) observed at or before p_as_of_ms (null = now),
-- ordered by instrument_id. Every column except observation_id; observed_at as observed_at_ms.
CREATE FUNCTION public.get_fx_symbol_specs(p_as_of_ms BIGINT DEFAULT NULL)
RETURNS JSONB
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public
AS $$
  SELECT coalesce(jsonb_agg(jsonb_build_object(
      'venue', l.venue, 'instrument_id', l.instrument_id, 'native_symbol', l.native_symbol,
      'digits', l.digits, 'point', l.point, 'tick_size', l.tick_size, 'tick_value', l.tick_value,
      'contract_size', l.contract_size, 'volume_min', l.volume_min, 'volume_max', l.volume_max,
      'volume_step', l.volume_step, 'base_currency', l.base_currency,
      'profit_currency', l.profit_currency, 'margin_currency', l.margin_currency,
      'account_currency', l.account_currency, 'trade_mode', l.trade_mode, 'swap_mode', l.swap_mode,
      'swap_long', l.swap_long, 'swap_short', l.swap_short,
      'swap_rollover3days', l.swap_rollover3days, 'spec_hash', l.spec_hash,
      'observed_at_ms', (extract(epoch FROM l.observed_at) * 1000)::BIGINT
    ) ORDER BY l.instrument_id, l.venue), '[]'::jsonb)
  FROM (
    SELECT DISTINCT ON (o.venue, o.instrument_id) o.*
    FROM public.fx_symbol_spec_observations o
    WHERE p_as_of_ms IS NULL
       OR o.observed_at <= timestamptz 'epoch' + p_as_of_ms * interval '1 millisecond'
    ORDER BY o.venue, o.instrument_id, o.observed_at DESC, o.observation_id DESC
  ) l;
$$;

REVOKE ALL ON FUNCTION
  public.record_fx_symbol_specs(JSONB),
  public.get_fx_symbol_specs(BIGINT)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION
  public.record_fx_symbol_specs(JSONB),
  public.get_fx_symbol_specs(BIGINT)
  TO service_role;
