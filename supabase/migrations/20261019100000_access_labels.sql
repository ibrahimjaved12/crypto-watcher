-- #411 Access labels (part of #374): nobody has access by default; one label per user: crypto, forex or all.
-- Roles and extra markets are removed (the role column was read only inside markets_of and carried no admin power).
-- Crypto access is enforced in the app (gate screen); forex is also enforced by the fx_* policies via has_market.

-- 1. The 'all' label.
INSERT INTO public.plans(plan,label) VALUES ('all','All markets');
INSERT INTO public.plan_markets(plan,market) VALUES ('all','crypto'),('all','forex');

-- 2. Lock every account (owner decision): all existing accounts, including the owner's, start with no access;
--    access is granted again with scripts/grant-access.mjs. No backfill. This also removes every row that
--    references the plans deleted in step 4.
DELETE FROM public.user_access;

-- 3. (no statement; numbering kept)

-- 4. Retire free and pro.
DELETE FROM public.plan_markets WHERE plan IN ('free','pro');
DELETE FROM public.plans WHERE plan IN ('free','pro');

-- 5. One label per user ('plan' stays the column name; "label" is the user-facing word).
ALTER TABLE public.user_access DROP COLUMN role, DROP COLUMN extra_markets, ALTER COLUMN plan DROP DEFAULT;

-- 6. No row or an expired row: no markets. Otherwise the label's markets, sorted.
CREATE OR REPLACE FUNCTION public.markets_of(p_user UUID)
RETURNS TEXT[] LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public AS $$
  SELECT CASE
    WHEN ua.user_id IS NULL OR (ua.expires_at IS NOT NULL AND ua.expires_at <= now()) THEN ARRAY[]::TEXT[]
    ELSE coalesce((SELECT array_agg(pm.market ORDER BY pm.market) FROM public.plan_markets pm WHERE pm.plan = ua.plan),
                  ARRAY[]::TEXT[])
  END
  FROM (SELECT 1) one LEFT JOIN public.user_access ua ON ua.user_id = p_user
$$;
REVOKE ALL ON FUNCTION public.markets_of(UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.markets_of(UUID) TO service_role;
