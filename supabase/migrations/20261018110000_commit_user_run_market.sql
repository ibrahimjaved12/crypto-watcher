-- #374 fix for 20261018100000: jsonb_populate_record gives NULL (not the column default) for keys the JSON lacks,
-- so the crypto commit_user_run inserted paper_runs.market = NULL and failed the NOT NULL check. Same body as in
-- 20261018100000 except that market='crypto' and fx_stage_run_id=NULL are set explicitly, after p_run.
CREATE OR REPLACE FUNCTION public.commit_user_run(p_user_id UUID,p_stage_run_id UUID,p_run JSONB,p_ledger JSONB,p_expected_run_id UUID)
RETURNS UUID LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE account public.forward_wallet_accounts; stage public.forward_market_stages;
  latest public.paper_runs; existing UUID; run_id UUID := gen_random_uuid(); cursor_ms BIGINT; item JSONB;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('forward-wallet:'||p_user_id::TEXT,0));
  SELECT * INTO account FROM public.forward_wallet_accounts WHERE user_id=p_user_id AND market='crypto';
  IF account.user_id IS NULL THEN RAISE EXCEPTION 'Account not initialized'; END IF;
  SELECT * INTO stage FROM public.forward_market_stages WHERE id=p_stage_run_id;
  IF stage.id IS NULL THEN RAISE EXCEPTION 'Market stage missing'; END IF;
  SELECT id INTO existing FROM public.paper_runs WHERE user_id=p_user_id AND stage_run_id=stage.id AND status='ok';
  IF existing IS NOT NULL THEN RETURN existing; END IF;
  SELECT * INTO latest FROM public.paper_runs WHERE user_id=p_user_id AND paper_account=account.account_id AND status='ok'
    ORDER BY boundary_ms DESC,created_at DESC,id DESC LIMIT 1;
  IF latest.id IS DISTINCT FROM p_expected_run_id THEN
    RAISE EXCEPTION 'Wallet changed; reload and retry' USING ERRCODE='40001';
  END IF;
  cursor_ms := greatest(account.start_boundary_ms,coalesce(latest.consumed_boundary_ms,latest.boundary_ms,0));
  IF stage.boundary_ms<=cursor_ms OR EXISTS(SELECT 1 FROM public.forward_market_stages
      WHERE boundary_ms>cursor_ms AND boundary_ms<stage.boundary_ms) THEN
    RAISE EXCEPTION 'Wallet stages must be consumed in order';
  END IF;
  IF p_run->>'status' NOT IN ('ok','skipped_stale','no_sigma') THEN RAISE EXCEPTION 'Invalid user run status'; END IF;
  IF p_run->>'status'<>'ok' AND jsonb_array_length(p_ledger)>0 THEN RAISE EXCEPTION 'Retryable ledger must be empty'; END IF;
  INSERT INTO public.paper_runs SELECT (jsonb_populate_record(NULL::public.paper_runs,
    jsonb_build_object('processed_to_ms','{}'::jsonb,'versions','{}'::jsonb,'assumptions','[]'::jsonb,
      'reasons','{}'::jsonb,'counts','{}'::jsonb,'freshness','{}'::jsonb) || p_run ||
    jsonb_build_object('id',run_id,'user_id',p_user_id,'paper_account',account.account_id,
      'market','crypto','fx_stage_run_id',NULL,'stage_run_id',stage.id,'boundary_ms',stage.boundary_ms,'run_key','hour:'||stage.boundary_ms,
      'consumed_boundary_ms',CASE WHEN p_run->>'status'='ok' THEN stage.boundary_ms ELSE NULL END,
      'created_at',clock_timestamp()))).*;
  FOR item IN SELECT value FROM jsonb_array_elements(p_ledger) LOOP
    INSERT INTO public.paper_ledger(user_id,paper_account,seq,ms,type,setup_id,symbol,amount_e8,balance_e8,payload,run_id)
      VALUES(p_user_id,account.account_id,(item->>'seq')::BIGINT,(item->>'ms')::BIGINT,item->>'type',
        item->>'setup_id',item->>'symbol',(item->>'amount_e8')::BIGINT,(item->>'balance_e8')::BIGINT,item,run_id);
  END LOOP;
  RETURN run_id;
END $$;
REVOKE ALL ON FUNCTION public.commit_user_run(UUID,UUID,JSONB,JSONB,UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.commit_user_run(UUID,UUID,JSONB,JSONB,UUID) TO service_role;
