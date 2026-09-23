-- Monitor-run retention needs an explicit product policy. Remove the helper that
-- coupled each insert to automatic history deletion. This does not delete data.
DROP FUNCTION IF EXISTS public.record_monitor_run(
  UUID, TEXT, INTEGER, INTEGER, TEXT, TEXT, INTEGER, JSONB
);
