select symbol, timeframe, candle_at, outcome_status
from public.ta_signals
order by candle_at desc
limit 20;