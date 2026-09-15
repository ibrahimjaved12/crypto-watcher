-- Polygon renamed MATIC to POL. Move active watchlist entries to the supported
-- POL market while preserving historical alerts and notes under their old symbol.
DELETE FROM public.watchlist_items AS matic
WHERE matic.symbol = 'MATICUSDT'
  AND EXISTS (
    SELECT 1
    FROM public.watchlist_items AS pol
    WHERE pol.user_id = matic.user_id
      AND pol.symbol = 'POLUSDT'
  );

UPDATE public.watchlist_items
SET symbol = 'POLUSDT'
WHERE symbol = 'MATICUSDT';

-- A baseline collected from the retired market must not trigger a POL alert.
DELETE FROM public.monitor_baselines
WHERE symbol = 'MATICUSDT';
