-- TA provenance: the actual exchange event time is provenance, not the definition of
-- candle completion. WebSocket klines carry a real exchange event time; REST
-- bootstrap/recovery has none, so the absence is recorded as NULL instead of an
-- invented boundary. Candle completion remains the deterministic boundary
-- `candle_at + timeframe`, enforced by the application's no-lookahead checks.
--
-- Replaces the conflating check from 20260924120000_python_scheduled_ta.sql, which
-- required `source_event_at = candle_at + timeframe`.

ALTER TABLE public.ta_signals ALTER COLUMN source_event_at DROP NOT NULL;

ALTER TABLE public.ta_signals DROP CONSTRAINT ta_signals_python_timestamps_check;
ALTER TABLE public.ta_signals ADD CONSTRAINT ta_signals_python_timestamps_check CHECK (
  (source_event_at IS NULL OR detected_at >= source_event_at)
  AND evaluated_at >= detected_at
);
