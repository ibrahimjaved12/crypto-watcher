-- #36 Part 2 (wallet-v3): the paper ledger gains order-lifecycle kinds and the two stop/target modification kinds
-- reserved for the exit-management work of #221 (nothing writes those yet). Existing rows keep their six kinds;
-- the ledger stays append-only (the UPDATE/DELETE trigger is untouched). Application databases created from the
-- schema files get the same list from this migration, applied after 20261009120000_forward_harness.sql.
DO $$
DECLARE
  existing TEXT;
BEGIN
  SELECT conname INTO existing
  FROM pg_constraint
  WHERE conrelid = 'public.paper_ledger'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%open_fee%';
  IF existing IS NOT NULL THEN
    EXECUTE format('ALTER TABLE public.paper_ledger DROP CONSTRAINT %I', existing);
  END IF;
END;
$$;

ALTER TABLE public.paper_ledger ADD CONSTRAINT paper_ledger_type_check CHECK (type IN (
  'open_fee', 'pnl', 'close_fee', 'funding', 'liquidation', 'rejected',
  'order_submitted', 'order_accepted', 'order_rejected', 'order_cancelled', 'order_expired',
  'order_filled', 'order_partially_filled', 'order_ambiguous',
  'stop_moved', 'target_moved'
));
