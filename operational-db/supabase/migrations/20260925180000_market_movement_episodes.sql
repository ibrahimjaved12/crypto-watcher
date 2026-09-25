-- Operational persistence for market-movement episodes and bounded current state (Issue #73).
-- Service-role only, external operational database.

CREATE TABLE public.market_state_current (
  universe_id TEXT NOT NULL,
  primary_window_minutes INTEGER NOT NULL CHECK (primary_window_minutes = 5),
  universe_version TEXT NOT NULL,
  provider TEXT NOT NULL CHECK (provider = 'binance-usdm'),
  exchange TEXT NOT NULL CHECK (exchange = 'binance'),
  price_type TEXT NOT NULL CHECK (price_type = 'trade'),
  evaluation_boundary_time TIMESTAMPTZ NOT NULL,
  direction_state TEXT NOT NULL CHECK (direction_state IN ('BROAD_RISE', 'BROAD_DROP', 'NEUTRAL', 'WARMING', 'UNAVAILABLE')),
  pace TEXT NOT NULL CHECK (pace IN ('ACCELERATING', 'DECELERATING', 'MIXED', 'NOT_APPLICABLE')),
  active_episode_id TEXT,
  active_direction TEXT CHECK (active_direction IS NULL OR active_direction IN ('BROAD_RISE', 'BROAD_DROP')),
  interrupted BOOLEAN NOT NULL DEFAULT false,
  classifier_algorithm_version TEXT NOT NULL,
  classifier_config_version TEXT NOT NULL,
  movement_algorithm_version TEXT NOT NULL,
  movement_config_version TEXT NOT NULL,
  pending_state JSONB NOT NULL DEFAULT '{}'::jsonb,
  current_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (universe_id, primary_window_minutes)
);

CREATE TABLE public.market_movement_events (
  event_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  transition TEXT NOT NULL CHECK (transition IN ('STARTED', 'STRENGTHENED', 'WEAKENED', 'REVERSED', 'ENDED')),
  transition_reason TEXT NOT NULL,
  from_direction TEXT CHECK (from_direction IS NULL OR from_direction IN ('BROAD_RISE', 'BROAD_DROP')),
  to_direction TEXT CHECK (to_direction IS NULL OR to_direction IN ('BROAD_RISE', 'BROAD_DROP')),
  episode_start_boundary_time TIMESTAMPTZ NOT NULL,
  evaluation_boundary_time TIMESTAMPTZ NOT NULL,
  universe_id TEXT NOT NULL,
  universe_version TEXT NOT NULL,
  primary_window_minutes INTEGER NOT NULL CHECK (primary_window_minutes = 5),
  provider TEXT NOT NULL CHECK (provider = 'binance-usdm'),
  exchange TEXT NOT NULL CHECK (exchange = 'binance'),
  price_type TEXT NOT NULL CHECK (price_type = 'trade'),
  direction TEXT NOT NULL CHECK (direction IN ('BROAD_RISE', 'BROAD_DROP')),
  pace TEXT NOT NULL CHECK (pace IN ('ACCELERATING', 'DECELERATING', 'MIXED', 'NOT_APPLICABLE')),
  directional_breadth DOUBLE PRECISION NOT NULL,
  material_breadth DOUBLE PRECISION NOT NULL,
  median_raw_return DOUBLE PRECISION,
  median_normalized_movement DOUBLE PRECISION,
  median_acceleration DOUBLE PRECISION,
  acceleration_breadth DOUBLE PRECISION NOT NULL,
  dispersion DOUBLE PRECISION,
  rvol_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  outliers JSONB NOT NULL DEFAULT '[]'::jsonb,
  supporting_contracts JSONB NOT NULL DEFAULT '[]'::jsonb,
  conflicting_contracts JSONB NOT NULL DEFAULT '[]'::jsonb,
  configured_universe JSONB NOT NULL DEFAULT '[]'::jsonb,
  included_symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
  excluded_symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
  windows_context JSONB NOT NULL DEFAULT '[]'::jsonb,
  classifier_algorithm_version TEXT NOT NULL,
  classifier_config_version TEXT NOT NULL,
  movement_algorithm_version TEXT NOT NULL,
  movement_config_version TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX market_movement_events_episode
  ON public.market_movement_events(episode_id, evaluation_boundary_time);

CREATE INDEX market_movement_events_boundary
  ON public.market_movement_events(evaluation_boundary_time DESC);

ALTER TABLE public.market_state_current ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.market_movement_events ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.market_state_current, public.market_movement_events
  FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.market_state_current, public.market_movement_events
  TO service_role;

CREATE FUNCTION public.upsert_market_state_current(
  p_universe_id TEXT,
  p_primary_window_minutes INTEGER,
  p_universe_version TEXT,
  p_provider TEXT,
  p_exchange TEXT,
  p_price_type TEXT,
  p_evaluation_boundary_time TIMESTAMPTZ,
  p_direction_state TEXT,
  p_pace TEXT,
  p_active_episode_id TEXT,
  p_active_direction TEXT,
  p_interrupted BOOLEAN,
  p_classifier_algorithm_version TEXT,
  p_classifier_config_version TEXT,
  p_movement_algorithm_version TEXT,
  p_movement_config_version TEXT,
  p_pending_state JSONB,
  p_current_evidence JSONB
) RETURNS VOID
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
BEGIN
  INSERT INTO public.market_state_current (
    universe_id, primary_window_minutes, universe_version, provider, exchange,
    price_type, evaluation_boundary_time, direction_state, pace,
    active_episode_id, active_direction, interrupted,
    classifier_algorithm_version, classifier_config_version,
    movement_algorithm_version, movement_config_version,
    pending_state, current_evidence, updated_at
  ) VALUES (
    p_universe_id, p_primary_window_minutes, p_universe_version, p_provider, p_exchange,
    p_price_type, p_evaluation_boundary_time, p_direction_state, p_pace,
    p_active_episode_id, p_active_direction, p_interrupted,
    p_classifier_algorithm_version, p_classifier_config_version,
    p_movement_algorithm_version, p_movement_config_version,
    coalesce(p_pending_state, '{}'::jsonb), coalesce(p_current_evidence, '{}'::jsonb),
    clock_timestamp()
  )
  ON CONFLICT (universe_id, primary_window_minutes) DO UPDATE SET
    universe_version = EXCLUDED.universe_version,
    provider = EXCLUDED.provider,
    exchange = EXCLUDED.exchange,
    price_type = EXCLUDED.price_type,
    evaluation_boundary_time = EXCLUDED.evaluation_boundary_time,
    direction_state = EXCLUDED.direction_state,
    pace = EXCLUDED.pace,
    active_episode_id = EXCLUDED.active_episode_id,
    active_direction = EXCLUDED.active_direction,
    interrupted = EXCLUDED.interrupted,
    classifier_algorithm_version = EXCLUDED.classifier_algorithm_version,
    classifier_config_version = EXCLUDED.classifier_config_version,
    movement_algorithm_version = EXCLUDED.movement_algorithm_version,
    movement_config_version = EXCLUDED.movement_config_version,
    pending_state = EXCLUDED.pending_state,
    current_evidence = EXCLUDED.current_evidence,
    updated_at = clock_timestamp();
END;
$$;

CREATE FUNCTION public.append_market_movement_event(
  p_event_id TEXT,
  p_episode_id TEXT,
  p_transition TEXT,
  p_transition_reason TEXT,
  p_from_direction TEXT,
  p_to_direction TEXT,
  p_episode_start_boundary_time TIMESTAMPTZ,
  p_evaluation_boundary_time TIMESTAMPTZ,
  p_universe_id TEXT,
  p_universe_version TEXT,
  p_primary_window_minutes INTEGER,
  p_provider TEXT,
  p_exchange TEXT,
  p_price_type TEXT,
  p_direction TEXT,
  p_pace TEXT,
  p_directional_breadth DOUBLE PRECISION,
  p_material_breadth DOUBLE PRECISION,
  p_median_raw_return DOUBLE PRECISION,
  p_median_normalized_movement DOUBLE PRECISION,
  p_median_acceleration DOUBLE PRECISION,
  p_acceleration_breadth DOUBLE PRECISION,
  p_dispersion DOUBLE PRECISION,
  p_rvol_summary JSONB,
  p_outliers JSONB,
  p_supporting_contracts JSONB,
  p_conflicting_contracts JSONB,
  p_configured_universe JSONB,
  p_included_symbols JSONB,
  p_excluded_symbols JSONB,
  p_windows_context JSONB,
  p_classifier_algorithm_version TEXT,
  p_classifier_config_version TEXT,
  p_movement_algorithm_version TEXT,
  p_movement_config_version TEXT
) RETURNS TEXT
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public
AS $$
DECLARE
  v_inserted TEXT;
BEGIN
  INSERT INTO public.market_movement_events (
    event_id, episode_id, transition, transition_reason,
    from_direction, to_direction, episode_start_boundary_time, evaluation_boundary_time,
    universe_id, universe_version, primary_window_minutes, provider, exchange,
    price_type, direction, pace, directional_breadth, material_breadth,
    median_raw_return, median_normalized_movement, median_acceleration,
    acceleration_breadth, dispersion, rvol_summary, outliers,
    supporting_contracts, conflicting_contracts, configured_universe,
    included_symbols, excluded_symbols, windows_context,
    classifier_algorithm_version, classifier_config_version,
    movement_algorithm_version, movement_config_version, created_at
  ) VALUES (
    p_event_id, p_episode_id, p_transition, p_transition_reason,
    p_from_direction, p_to_direction, p_episode_start_boundary_time, p_evaluation_boundary_time,
    p_universe_id, p_universe_version, p_primary_window_minutes, p_provider, p_exchange,
    p_price_type, p_direction, p_pace, p_directional_breadth, p_material_breadth,
    p_median_raw_return, p_median_normalized_movement, p_median_acceleration,
    p_acceleration_breadth, p_dispersion, coalesce(p_rvol_summary, '{}'::jsonb),
    coalesce(p_outliers, '[]'::jsonb), coalesce(p_supporting_contracts, '[]'::jsonb),
    coalesce(p_conflicting_contracts, '[]'::jsonb), coalesce(p_configured_universe, '[]'::jsonb),
    coalesce(p_included_symbols, '[]'::jsonb), coalesce(p_excluded_symbols, '[]'::jsonb),
    coalesce(p_windows_context, '[]'::jsonb),
    p_classifier_algorithm_version, p_classifier_config_version,
    p_movement_algorithm_version, p_movement_config_version, clock_timestamp()
  )
  ON CONFLICT (event_id) DO NOTHING
  RETURNING event_id INTO v_inserted;

  IF v_inserted IS NOT NULL THEN
    RETURN 'appended';
  ELSE
    RETURN 'already_exists';
  END IF;
END;
$$;
