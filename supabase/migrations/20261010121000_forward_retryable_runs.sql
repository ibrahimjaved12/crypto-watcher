-- Preserve skipped attempts as audit rows without consuming an hourly claim.
ALTER TABLE public.paper_runs DROP CONSTRAINT paper_runs_status_check;
ALTER TABLE public.paper_runs ADD CONSTRAINT paper_runs_status_check
  CHECK (status IN ('ok', 'skipped_stale', 'no_sigma', 'failed'));
ALTER TABLE public.paper_runs DROP CONSTRAINT paper_runs_user_id_paper_account_run_key_key;
CREATE UNIQUE INDEX paper_runs_completed_claim
  ON public.paper_runs (user_id, paper_account, run_key)
  WHERE status NOT IN ('skipped_stale', 'no_sigma');
