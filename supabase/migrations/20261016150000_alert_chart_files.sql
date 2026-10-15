-- Hosted copy of movement-alert setup charts (#298). The PNG is rendered on the owner's machine
-- (#299) and stored in a PRIVATE bucket; this table holds only metadata and the display state.
-- Image bytes never go into a database column. Only the service role writes (TanStack server code
-- and the machine upload hook); a signed-in user can read the metadata of their own charts.

CREATE TABLE public.alert_chart_files (
  alert_id UUID PRIMARY KEY REFERENCES public.alerts(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'rendering', 'ready', 'failed', 'capped')),
  rendered_local BOOLEAN NOT NULL DEFAULT false,
  error TEXT CHECK (error IS NULL OR length(error) <= 300),
  storage_path TEXT,
  sha256 TEXT CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
  bytes INTEGER CHECK (bytes IS NULL OR bytes BETWEEN 1 AND 524288),
  rendered_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT alert_chart_files_ready_has_object CHECK (
    status <> 'ready' OR (storage_path IS NOT NULL AND sha256 IS NOT NULL AND bytes IS NOT NULL
                          AND rendered_at IS NOT NULL)
  ),
  CONSTRAINT alert_chart_files_path_is_owned CHECK (
    storage_path IS NULL OR storage_path = user_id::TEXT || '/' || alert_id::TEXT || '.png'
  )
);

COMMENT ON TABLE public.alert_chart_files IS
  'Display state and storage metadata of the setup chart of a movement alert (#298). The PNG is in the private bucket alert-charts under <user_id>/<alert_id>.png; it was rendered on the owner machine and contains candles and this account''s baseline/alert prices only.';
COMMENT ON COLUMN public.alert_chart_files.status IS
  'pending (waiting for the machine, or rendered locally and not uploaded yet: see rendered_local), rendering, ready (object uploaded), failed (render failed, error holds a short message), capped (the per-account chart cap refused the upload).';
COMMENT ON COLUMN public.alert_chart_files.rendered_local IS 'The machine rendered the PNG locally but it is not (yet) uploaded.';
COMMENT ON COLUMN public.alert_chart_files.sha256 IS 'sha256 of the PNG as reported by the machine; the object size is verified, the hash is not recomputed on the Worker.';

ALTER TABLE public.alert_chart_files ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.alert_chart_files FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.alert_chart_files TO authenticated;
GRANT ALL ON public.alert_chart_files TO service_role;
CREATE POLICY alert_chart_files_select_own ON public.alert_chart_files FOR SELECT TO authenticated
  USING (user_id = auth.uid());
-- No user INSERT/UPDATE/DELETE policy. Rows go away with their alert (ON DELETE CASCADE).

-- Private bucket: no public URL; the site reads through short-lived signed URLs only.
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('alert-charts', 'alert-charts', false, 524288, ARRAY['image/png'])
ON CONFLICT (id) DO NOTHING;

-- The movement capture (on the machine) registers the chart as pending when the alert is saved.
CREATE FUNCTION public.register_alert_chart_pending(p_alert_id UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  INSERT INTO public.alert_chart_files (alert_id, user_id)
    SELECT a.id, a.user_id FROM public.alerts a WHERE a.id = p_alert_id AND NOT a.is_test
  ON CONFLICT (alert_id) DO NOTHING;
  RETURN FOUND;
END;
$$;

-- Start an upload: the alert must exist; the per-account cap counts uploaded charts and bytes (a
-- replacement of this alert's own chart does not count twice). At the cap the row says 'capped' and
-- nothing is deleted. Returns the owner's storage path.
CREATE FUNCTION public.begin_alert_chart_upload(
  p_alert_id UUID, p_bytes INTEGER, p_max_charts INTEGER, p_max_bytes BIGINT
)
RETURNS TABLE (ok BOOLEAN, reason TEXT, storage_path TEXT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
#variable_conflict use_column
DECLARE
  owner UUID;
  charts INTEGER;
  used BIGINT;
BEGIN
  IF p_bytes IS NULL OR p_bytes < 1 OR p_bytes > 524288 THEN
    RETURN QUERY SELECT false, 'bad_size'::TEXT, NULL::TEXT;
    RETURN;
  END IF;
  IF p_max_charts IS NULL OR p_max_charts < 1 OR p_max_bytes IS NULL OR p_max_bytes < 1 THEN
    RAISE EXCEPTION 'Invalid alert chart cap';
  END IF;
  SELECT a.user_id INTO owner FROM public.alerts a WHERE a.id = p_alert_id AND NOT a.is_test;
  IF owner IS NULL THEN
    RETURN QUERY SELECT false, 'alert_gone'::TEXT, NULL::TEXT;
    RETURN;
  END IF;
  SELECT count(*), coalesce(sum(f.bytes), 0) INTO charts, used FROM public.alert_chart_files f
    WHERE f.user_id = owner AND f.status = 'ready' AND f.alert_id <> p_alert_id;
  IF charts >= p_max_charts OR used + p_bytes > p_max_bytes THEN
    INSERT INTO public.alert_chart_files (alert_id, user_id, status, error)
      VALUES (p_alert_id, owner, 'capped', 'Your stored chart limit is reached')
      ON CONFLICT (alert_id) DO UPDATE SET status = 'capped', error = 'Your stored chart limit is reached',
        updated_at = now() WHERE public.alert_chart_files.status <> 'ready';
    RETURN QUERY SELECT false, 'cap'::TEXT, NULL::TEXT;
    RETURN;
  END IF;
  INSERT INTO public.alert_chart_files (alert_id, user_id) VALUES (p_alert_id, owner)
    ON CONFLICT (alert_id) DO NOTHING;
  RETURN QUERY SELECT true, 'ok'::TEXT, owner::TEXT || '/' || p_alert_id::TEXT || '.png';
END;
$$;

-- The object is in storage (its size was checked by the caller): publish it.
CREATE FUNCTION public.complete_alert_chart_upload(p_alert_id UUID, p_sha256 TEXT, p_bytes INTEGER)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  UPDATE public.alert_chart_files f SET status = 'ready', rendered_local = false, error = NULL,
      storage_path = f.user_id::TEXT || '/' || f.alert_id::TEXT || '.png', sha256 = p_sha256, bytes = p_bytes,
      rendered_at = now(), updated_at = now()
    WHERE f.alert_id = p_alert_id;
  RETURN FOUND;
END;
$$;

-- Machine progress: rendering, failed (short error), or pending with the PNG rendered locally. A ready
-- chart is never downgraded.
CREATE FUNCTION public.report_alert_chart_status(
  p_alert_id UUID, p_status TEXT, p_error TEXT, p_rendered_local BOOLEAN
)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  IF p_status NOT IN ('pending', 'rendering', 'failed') THEN
    RAISE EXCEPTION 'Invalid alert chart status';
  END IF;
  INSERT INTO public.alert_chart_files (alert_id, user_id, status, error, rendered_local)
    SELECT a.id, a.user_id, p_status,
           CASE WHEN p_status = 'failed' THEN left(coalesce(p_error, 'Render failed'), 300) END,
           coalesce(p_rendered_local, false)
    FROM public.alerts a WHERE a.id = p_alert_id AND NOT a.is_test
  ON CONFLICT (alert_id) DO UPDATE SET status = excluded.status, error = excluded.error,
      rendered_local = excluded.rendered_local, updated_at = now()
    WHERE public.alert_chart_files.status <> 'ready';
  RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION public.register_alert_chart_pending(UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.begin_alert_chart_upload(UUID, INTEGER, INTEGER, BIGINT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.complete_alert_chart_upload(UUID, TEXT, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.report_alert_chart_status(UUID, TEXT, TEXT, BOOLEAN) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.register_alert_chart_pending(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.begin_alert_chart_upload(UUID, INTEGER, INTEGER, BIGINT) TO service_role;
GRANT EXECUTE ON FUNCTION public.complete_alert_chart_upload(UUID, TEXT, INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.report_alert_chart_status(UUID, TEXT, TEXT, BOOLEAN) TO service_role;
