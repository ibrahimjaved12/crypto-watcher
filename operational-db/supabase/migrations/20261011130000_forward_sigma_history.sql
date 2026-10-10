CREATE FUNCTION public.get_collector_forward_history_start(p_symbol TEXT)
RETURNS BIGINT LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT (extract(epoch FROM min(open_time)) * 1000)::BIGINT
  FROM public.collector_recent_candles
  WHERE provider = 'binance-usdm' AND symbol = upper(p_symbol)
    AND price_type = 'trade' AND timeframe_minutes = 1;
$$;
REVOKE ALL ON FUNCTION public.get_collector_forward_history_start(TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_collector_forward_history_start(TEXT) TO service_role;
