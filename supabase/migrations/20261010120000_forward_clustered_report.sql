-- Day-clustered report: exact numeric residual sums in micro-R squared; UTC epoch days.
-- Keep old moments for CSV audit. Preserve RLS, filters and matched-control semantics.
DROP FUNCTION public.forward_outcome_report(BIGINT, BIGINT, TEXT[], TEXT[], INTEGER[], INTEGER[], TEXT[]);

CREATE FUNCTION public.forward_outcome_report(
  p_from_ms BIGINT, p_to_ms BIGINT, p_strategy_ids TEXT[], p_symbols TEXT[],
  p_horizons INTEGER[], p_sides INTEGER[], p_rr TEXT[]
)
RETURNS TABLE (
  strategy_id TEXT, version TEXT, horizon_min INTEGER, rr TEXT,
  n BIGINT, n_t BIGINT, n_s BIGINT, n_e BIGINT, n_l BIGINT, n_x BIGINT, n_ambiguous BIGINT,
  sum_net_ur NUMERIC, sum_sq_net_ur NUMERIC, sum_cost_ur NUMERIC, sum_fund_ur NUMERIC,
  first_exit_ms BIGINT, last_exit_ms BIGINT,
  placebo_n BIGINT, placebo_sum_net_ur NUMERIC, placebo_sum_sq_net_ur NUMERIC,
  exit_days BIGINT, cluster_sum_sq_ur NUMERIC, placebo_exit_days BIGINT, placebo_cluster_sum_sq_ur NUMERIC
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  WITH f AS (
    SELECT s.strategy_id, s.version, s.horizon_min, s.rr, o.status, o.net_ur, o.cost_ur, o.fund_ur, o.exit_ms
    FROM public.forward_outcomes o
    JOIN public.forward_setups s ON s.user_id = o.user_id AND s.setup_id = o.setup_id
    WHERE o.final AND o.net_ur IS NOT NULL
      AND (p_from_ms IS NULL OR o.exit_ms >= p_from_ms)
      AND (p_to_ms IS NULL OR o.exit_ms < p_to_ms)
      AND (coalesce(cardinality(p_symbols), 0) = 0 OR s.symbol = ANY (p_symbols))
      AND (coalesce(cardinality(p_horizons), 0) = 0 OR s.horizon_min = ANY (p_horizons))
      AND (coalesce(cardinality(p_sides), 0) = 0 OR s.side = ANY (p_sides))
      AND (coalesce(cardinality(p_rr), 0) = 0 OR s.rr = ANY (p_rr))
  ),
  rl AS (
    SELECT f.strategy_id, f.version, f.horizon_min, f.rr,
           count(*) AS n,
           count(*) FILTER (WHERE f.status = 'T') AS n_t,
           count(*) FILTER (WHERE f.status = 'S') AS n_s,
           count(*) FILTER (WHERE f.status = 'E') AS n_e,
           count(*) FILTER (WHERE f.status = 'L') AS n_l,
           count(*) FILTER (WHERE f.status = 'X') AS n_x,
           count(*) FILTER (WHERE f.status = 'ambiguous') AS n_ambiguous,
           sum(f.net_ur::numeric) AS sum_net_ur,
           sum(f.net_ur::numeric * f.net_ur::numeric) AS sum_sq_net_ur,
           coalesce(sum(f.cost_ur::numeric), 0) AS sum_cost_ur,
           coalesce(sum(f.fund_ur::numeric), 0) AS sum_fund_ur,
           min(f.exit_ms) AS first_exit_ms, max(f.exit_ms) AS last_exit_ms
    FROM f
    WHERE f.strategy_id NOT LIKE 'placebo-v1:%'
      AND (coalesce(cardinality(p_strategy_ids), 0) = 0 OR f.strategy_id = ANY (p_strategy_ids))
    GROUP BY f.strategy_id, f.version, f.horizon_min, f.rr
  ),
  control AS (
    SELECT f.strategy_id, f.horizon_min, f.rr, count(*) AS n,
           sum(f.net_ur::numeric) AS sum_net_ur, sum(f.net_ur::numeric * f.net_ur::numeric) AS sum_sq_net_ur
    FROM f
    WHERE f.strategy_id LIKE 'placebo-v1:%'
    GROUP BY f.strategy_id, f.horizon_min, f.rr
  ),
  real_days AS (
    SELECT f.strategy_id, f.version, f.horizon_min, f.rr, f.exit_ms / 86400000 AS day,
           count(*) AS n, sum(f.net_ur::numeric) AS s
    FROM f WHERE f.exit_ms IS NOT NULL AND f.strategy_id NOT LIKE 'placebo-v1:%'
    GROUP BY f.strategy_id, f.version, f.horizon_min, f.rr, f.exit_ms / 86400000
  ),
  control_days AS (
    SELECT f.strategy_id, f.horizon_min, f.rr, f.exit_ms / 86400000 AS day,
           count(*) AS n, sum(f.net_ur::numeric) AS s
    FROM f WHERE f.exit_ms IS NOT NULL AND f.strategy_id LIKE 'placebo-v1:%'
    GROUP BY f.strategy_id, f.horizon_min, f.rr, f.exit_ms / 86400000
  ),
  real_clusters AS (
    SELECT r.strategy_id, r.version, r.horizon_min, r.rr, count(*) AS days,
           CASE WHEN sum(d.n) = r.n THEN sum(power(d.s - d.n * r.sum_net_ur / r.n, 2)) END AS ss
    FROM rl r JOIN real_days d USING (strategy_id, version, horizon_min, rr)
    GROUP BY r.strategy_id, r.version, r.horizon_min, r.rr, r.n, r.sum_net_ur
  ),
  control_clusters AS (
    SELECT c.strategy_id, c.horizon_min, c.rr, count(*) AS days,
           CASE WHEN sum(d.n) = c.n THEN sum(power(d.s - d.n * c.sum_net_ur / c.n, 2)) END AS ss
    FROM control c JOIN control_days d USING (strategy_id, horizon_min, rr)
    GROUP BY c.strategy_id, c.horizon_min, c.rr, c.n, c.sum_net_ur
  )
  SELECT r.strategy_id, r.version, r.horizon_min, r.rr,
         r.n, r.n_t, r.n_s, r.n_e, r.n_l, r.n_x, r.n_ambiguous,
         r.sum_net_ur, r.sum_sq_net_ur, r.sum_cost_ur, r.sum_fund_ur, r.first_exit_ms, r.last_exit_ms,
         coalesce(c.n, 0), coalesce(c.sum_net_ur, 0), coalesce(c.sum_sq_net_ur, 0),
         coalesce(rd.days, 0), rd.ss, coalesce(cd.days, 0), cd.ss
  FROM rl r
  LEFT JOIN control c
    ON c.strategy_id = 'placebo-v1:' || r.strategy_id AND c.horizon_min = r.horizon_min AND c.rr = r.rr
  LEFT JOIN real_clusters rd ON rd.strategy_id = r.strategy_id AND rd.version = r.version
    AND rd.horizon_min = r.horizon_min AND rd.rr = r.rr
  LEFT JOIN control_clusters cd
    ON cd.strategy_id = c.strategy_id AND cd.horizon_min = c.horizon_min AND cd.rr = c.rr
  ORDER BY r.n DESC, r.strategy_id, r.horizon_min, r.rr
$$;

REVOKE ALL ON FUNCTION public.forward_outcome_report(BIGINT, BIGINT, TEXT[], TEXT[], INTEGER[], INTEGER[], TEXT[]) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.forward_outcome_report(BIGINT, BIGINT, TEXT[], TEXT[], INTEGER[], INTEGER[], TEXT[]) TO authenticated, service_role;
