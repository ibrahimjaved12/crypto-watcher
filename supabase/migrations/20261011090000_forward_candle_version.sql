-- Candle conflict policy (#239 P15): every forward signal and setup records the candle_version it
-- was decided on: SHA-256 over the operational collector_candle_hash values (candle-v1) of the
-- completed 1-minute candles that form the decision candle (the strategy timeframe ending at
-- signal_ms). A later REST revision (operational collector_candle_revisions.supersedes) can then be
-- traced to the decisions that used the original candle. NULL for rows recorded before this column
-- existed or when a decision candle was incomplete. Additive; the tables stay append-only.
ALTER TABLE public.forward_signals
  ADD COLUMN candle_version TEXT
  CHECK (candle_version IS NULL OR candle_version ~ '^[0-9a-f]{64}$');
ALTER TABLE public.forward_setups
  ADD COLUMN candle_version TEXT
  CHECK (candle_version IS NULL OR candle_version ~ '^[0-9a-f]{64}$');

COMMENT ON COLUMN public.forward_signals.candle_version IS
  'SHA-256 of the candle-v1 hashes of the 1m candles forming the decision candle (P15); NULL when unknown.';
COMMENT ON COLUMN public.forward_setups.candle_version IS
  'candle_version of the setup''s signal (P15); NULL when unknown.';
