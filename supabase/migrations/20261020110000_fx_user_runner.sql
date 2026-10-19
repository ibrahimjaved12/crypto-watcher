-- Forex per-user pass (#446): the default paper-account config stored in the account, and the list of users
-- entitled to forex. Never edits an applied migration (ensure_fx_account is replaced with the same signature,
-- so its grants stay).

-- Owner decisions 2026-10-19: 1,000 USD start, 1 % risk per trade, effectively no position-count cap,
-- total open notional <= 10 x balance. The keys are the ones POST /v1/fx/forward/wallet_step accepts.
CREATE FUNCTION public.fx_default_wallet_config()
RETURNS JSONB LANGUAGE sql IMMUTABLE SET search_path=public AS $$
  SELECT jsonb_build_object('initial_balance_e8',100000000000,'risk_fraction','1/100',
                            'max_positions',1000,'max_exposure_multiple','10')
$$;

CREATE OR REPLACE FUNCTION public.ensure_fx_account(p_user_id UUID,p_boundary_ms BIGINT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-wallet:'||p_user_id::TEXT||':forex',0));
  IF NOT ('forex' = ANY(public.markets_of(p_user_id))) THEN
    RAISE EXCEPTION 'User is not entitled to forex' USING ERRCODE='42501';
  END IF;
  INSERT INTO public.forward_wallet_accounts(user_id,market,account_id,start_boundary_ms,config)
    VALUES(p_user_id,'forex',p_user_id::TEXT||':forex',p_boundary_ms,public.fx_default_wallet_config())
    ON CONFLICT DO NOTHING;
  -- An account created before this migration has the column default '{}': give it the defaults once.
  UPDATE public.forward_wallet_accounts SET config=public.fx_default_wallet_config()
    WHERE user_id=p_user_id AND market='forex' AND config='{}'::jsonb;
END $$;

-- Users whose plan (or extra markets, or role) includes forex right now; markets_of is the single source.
CREATE FUNCTION public.fx_entitled_users()
RETURNS SETOF UUID LANGUAGE sql STABLE SECURITY INVOKER SET search_path=public AS $$
  SELECT ua.user_id FROM public.user_access ua WHERE 'forex' = ANY(public.markets_of(ua.user_id)) ORDER BY ua.user_id
$$;

UPDATE public.forward_wallet_accounts SET config=public.fx_default_wallet_config()
  WHERE market='forex' AND config='{}'::jsonb;

REVOKE ALL ON FUNCTION public.fx_default_wallet_config(),public.fx_entitled_users() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.fx_default_wallet_config(),public.fx_entitled_users() TO service_role;
