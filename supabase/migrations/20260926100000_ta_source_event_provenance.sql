-- Pre-release destructive cutover for TA provenance. Lovable (application) database.
--
-- THIS IS A DESTRUCTIVE PRE-RELEASE RESET, NOT A PRODUCTION-COMPATIBLE MIGRATION.
-- Existing `ta_signals` rows were persisted under the old semantics, where `source_event_at`
-- was a synthetic boundary (`candle_at + timeframe`). They are disposable development data and
-- are cleared here so only the truthful semantics remain; no compatibility column and no
-- legacy-version marker is added.
--
-- Truthful timestamp semantics (three distinct concepts, never conflated):
--   * completion boundary = candle_at + timeframe
--   * source_event_at     = the actual Binance exchange event time (WebSocket `E`), or NULL for
--                           REST bootstrap/recovery, which has no exchange event
--   * detected_at / evaluated_at = the actual detection/evaluation times
--
-- The TA formula and strategy versions are unchanged: only provenance semantics and the API
-- schema version change.

-- `ta_signals` is immutable by trigger; disable it for the one-time reset, then restore it.
ALTER TABLE public.ta_signals DISABLE TRIGGER ta_conclusion_immutable;
DELETE FROM public.ta_signals;
ALTER TABLE public.ta_signals ENABLE TRIGGER ta_conclusion_immutable;

-- The exchange event time is provenance, so REST-derived rows legitimately have none.
ALTER TABLE public.ta_signals ALTER COLUMN source_event_at DROP NOT NULL;

-- Replaces the conflating check from 20260924120000_python_scheduled_ta.sql, which required
-- `source_event_at = candle_at + timeframe`. Finality/no-lookahead is now the deterministic
-- completion boundary, enforced by the application and Python contract, not by this check.
ALTER TABLE public.ta_signals DROP CONSTRAINT ta_signals_python_timestamps_check;
ALTER TABLE public.ta_signals ADD CONSTRAINT ta_signals_python_timestamps_check CHECK (
  (source_event_at IS NULL OR detected_at >= source_event_at)
  AND evaluated_at >= detected_at
);
