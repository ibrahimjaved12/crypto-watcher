-- #36 Part 3 (Paper account page): ONE row of totals over every ledger line of the caller's current wallet account,
-- computed in SQL so the page never depends on how many ledger pages it has loaded (no row cap). A trade is a setup
-- whose position is closed: a `liquidation` line, or a `pnl` line that is not a partial reduction. Its net is the sum
-- of its fee, funding, pnl and liquidation lines. SECURITY INVOKER: own rows through RLS (like paper_equity_series).
CREATE FUNCTION public.paper_account_summary()
RETURNS TABLE (
  trades BIGINT, target BIGINT, stopped BIGINT, expired BIGINT, liquidated BIGINT, ambiguous BIGINT,
  unresolved BIGINT, wins BIGINT, losses BIGINT,
  sum_net_e8 NUMERIC, gross_wins_e8 NUMERIC, gross_losses_e8 NUMERIC,
  rejected BIGINT, order_rejected BIGINT,
  open_fees_e8 NUMERIC, close_fees_e8 NUMERIC, funding_e8 NUMERIC, liquidation_e8 NUMERIC, pnl_e8 NUMERIC,
  lines BIGINT, first_ms BIGINT, last_ms BIGINT
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  WITH acct AS (
    SELECT l.*
    FROM public.paper_ledger l
    WHERE EXISTS (SELECT 1 FROM public.forward_wallet_accounts a
                  WHERE a.user_id = l.user_id AND a.account_id = l.paper_account)
  ),
  closing AS (
    SELECT DISTINCT ON (c.setup_id) c.setup_id, c.type, c.payload ->> 'outcome' AS outcome
    FROM acct c
    WHERE c.setup_id IS NOT NULL AND c.type IN ('pnl', 'liquidation')
      AND (c.payload ->> 'position_event' IS NULL OR c.payload ->> 'position_event' = 'closed')
    ORDER BY c.setup_id, c.seq DESC
  ),
  trade AS (
    SELECT c.setup_id, c.type, c.outcome, sum(m.amount_e8) AS net
    FROM closing c
    JOIN acct m ON m.setup_id = c.setup_id
               AND m.type IN ('open_fee', 'pnl', 'close_fee', 'funding', 'liquidation')
    GROUP BY c.setup_id, c.type, c.outcome
  )
  SELECT
    (SELECT count(*) FROM trade),
    (SELECT count(*) FROM trade WHERE type = 'pnl' AND outcome = 'T'),
    (SELECT count(*) FROM trade WHERE type = 'pnl' AND outcome = 'S'),
    (SELECT count(*) FROM trade WHERE type = 'pnl' AND outcome = 'E'),
    (SELECT count(*) FROM trade WHERE type = 'liquidation'),
    (SELECT count(*) FROM trade WHERE type = 'pnl' AND outcome = 'ambiguous'),
    (SELECT count(*) FROM trade WHERE type = 'pnl' AND (outcome IS NULL OR outcome NOT IN ('T', 'S', 'E', 'ambiguous'))),
    (SELECT count(*) FROM trade WHERE net > 0),
    (SELECT count(*) FROM trade WHERE net < 0),
    (SELECT coalesce(sum(net), 0) FROM trade),
    (SELECT coalesce(sum(net) FILTER (WHERE net > 0), 0) FROM trade),
    (SELECT coalesce(sum(net) FILTER (WHERE net < 0), 0) FROM trade),
    (SELECT count(*) FROM acct WHERE type = 'rejected'),
    (SELECT count(*) FROM acct WHERE type = 'order_rejected'),
    (SELECT coalesce(sum(amount_e8), 0) FROM acct WHERE type = 'open_fee'),
    (SELECT coalesce(sum(amount_e8), 0) FROM acct WHERE type = 'close_fee'),
    (SELECT coalesce(sum(amount_e8), 0) FROM acct WHERE type = 'funding'),
    (SELECT coalesce(sum(amount_e8), 0) FROM acct WHERE type = 'liquidation'),
    (SELECT coalesce(sum(amount_e8), 0) FROM acct WHERE type = 'pnl'),
    (SELECT count(*) FROM acct),
    (SELECT min(ms) FROM acct),
    (SELECT max(ms) FROM acct)
$$;

REVOKE ALL ON FUNCTION public.paper_account_summary() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.paper_account_summary() TO authenticated;
