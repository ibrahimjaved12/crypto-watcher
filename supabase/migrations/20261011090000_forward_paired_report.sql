-- Paired-by-exit-day difference (strategy minus its matched placebo control). Same filters, RLS and
-- matched-control semantics as forward_outcome_report; a separate function so that report keeps its
-- shape. For one row (strategy, version, timeframe, reward:risk) with real trades n, sum S, control
-- trades m, sum C, and day sums s_d, c_d with counts n_d, m_d over the UNION of UTC exit days (a missing
-- side counts as 0):
--   r_d = (s_d - n_d * S/n) / n  -  (c_d - m_d * C/m) / m      (micro-R)
--   paired_ss = sum_d r_d^2 ;  SE(D) = sqrt(paired_ss) for D = S/n - C/m
-- The same-day covariance of strategy and control stays inside r_d. paired_days counts the days on which
-- BOTH series have a trade; union_days all days of either series.
CREATE FUNCTION public.forward_paired_report(
  p_from_ms BIGINT, p_to_ms BIGINT, p_strategy_ids TEXT[], p_symbols TEXT[],
  p_horizons INTEGER[], p_sides INTEGER[], p_rr TEXT[]
)
RETURNS TABLE (
  strategy_id TEXT, version TEXT, horizon_min INTEGER, rr TEXT,
  paired_days BIGINT, union_days BIGINT, paired_ss NUMERIC
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  WITH f AS (
    SELECT s.strategy_id, s.version, s.horizon_min, s.rr, o.net_ur, o.exit_ms
    FROM public.forward_outcomes o
    JOIN public.forward_setups s ON s.user_id = o.user_id AND s.setup_id = o.setup_id
    WHERE o.final AND o.net_ur IS NOT NULL AND o.exit_ms IS NOT NULL
      AND (p_from_ms IS NULL OR o.exit_ms >= p_from_ms)
      AND (p_to_ms IS NULL OR o.exit_ms < p_to_ms)
      AND (coalesce(cardinality(p_symbols), 0) = 0 OR s.symbol = ANY (p_symbols))
      AND (coalesce(cardinality(p_horizons), 0) = 0 OR s.horizon_min = ANY (p_horizons))
      AND (coalesce(cardinality(p_sides), 0) = 0 OR s.side = ANY (p_sides))
      AND (coalesce(cardinality(p_rr), 0) = 0 OR s.rr = ANY (p_rr))
  ),
  rd AS (
    SELECT f.strategy_id, f.version, f.horizon_min, f.rr, f.exit_ms / 86400000 AS day,
           count(*) AS n, sum(f.net_ur::numeric) AS s
    FROM f
    WHERE f.strategy_id NOT LIKE 'placebo-v1:%'
      AND (coalesce(cardinality(p_strategy_ids), 0) = 0 OR f.strategy_id = ANY (p_strategy_ids))
    GROUP BY f.strategy_id, f.version, f.horizon_min, f.rr, f.exit_ms / 86400000
  ),
  cd AS (
    SELECT f.strategy_id, f.horizon_min, f.rr, f.exit_ms / 86400000 AS day,
           count(*) AS n, sum(f.net_ur::numeric) AS s
    FROM f
    WHERE f.strategy_id LIKE 'placebo-v1:%'
    GROUP BY f.strategy_id, f.horizon_min, f.rr, f.exit_ms / 86400000
  ),
  rt AS (
    SELECT d.strategy_id, d.version, d.horizon_min, d.rr, sum(d.n) AS n, sum(d.s) AS s
    FROM rd d GROUP BY d.strategy_id, d.version, d.horizon_min, d.rr
  ),
  ct AS (
    SELECT d.strategy_id, d.horizon_min, d.rr, sum(d.n) AS n, sum(d.s) AS s
    FROM cd d GROUP BY d.strategy_id, d.horizon_min, d.rr
  ),
  k AS (
    SELECT r.strategy_id, r.version, r.horizon_min, r.rr, r.n AS rn, r.s AS rs, c.n AS cn, c.s AS cs
    FROM rt r JOIN ct c
      ON c.strategy_id = 'placebo-v1:' || r.strategy_id AND c.horizon_min = r.horizon_min AND c.rr = r.rr
  ),
  days AS (
    SELECT k.strategy_id, k.version, k.horizon_min, k.rr, u.day
    FROM k
    CROSS JOIN LATERAL (
      SELECT d.day FROM rd d
       WHERE d.strategy_id = k.strategy_id AND d.version = k.version AND d.horizon_min = k.horizon_min AND d.rr = k.rr
      UNION
      SELECT d.day FROM cd d
       WHERE d.strategy_id = 'placebo-v1:' || k.strategy_id AND d.horizon_min = k.horizon_min AND d.rr = k.rr
    ) u
  ),
  terms AS (
    SELECT k.strategy_id, k.version, k.horizon_min, k.rr,
           (a.n IS NOT NULL AND b.n IS NOT NULL) AS both_traded,
           (coalesce(a.s, 0) - coalesce(a.n, 0) * k.rs / k.rn) / k.rn
             - (coalesce(b.s, 0) - coalesce(b.n, 0) * k.cs / k.cn) / k.cn AS r
    FROM days dy
    JOIN k USING (strategy_id, version, horizon_min, rr)
    LEFT JOIN rd a ON a.strategy_id = k.strategy_id AND a.version = k.version AND a.horizon_min = k.horizon_min
      AND a.rr = k.rr AND a.day = dy.day
    LEFT JOIN cd b ON b.strategy_id = 'placebo-v1:' || k.strategy_id AND b.horizon_min = k.horizon_min
      AND b.rr = k.rr AND b.day = dy.day
  )
  SELECT t.strategy_id, t.version, t.horizon_min, t.rr,
         count(*) FILTER (WHERE t.both_traded), count(*), sum(t.r * t.r)
  FROM terms t
  GROUP BY t.strategy_id, t.version, t.horizon_min, t.rr
$$;

REVOKE ALL ON FUNCTION public.forward_paired_report(BIGINT, BIGINT, TEXT[], TEXT[], INTEGER[], INTEGER[], TEXT[]) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.forward_paired_report(BIGINT, BIGINT, TEXT[], TEXT[], INTEGER[], INTEGER[], TEXT[]) TO authenticated, service_role;
