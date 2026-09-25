import { validateEnvironment, validatePublicSecrets, validateServerSupabase } from "./environment";

/**
 * TanStack application-server validation.
 *
 * The app serves a browser bundle built with `VITE_*` values, so this proves the
 * runtime server configuration matches that browser build. Only the application can
 * make this comparison: a headless backend process has no browser bundle. Called by
 * the server entry and the authenticated middleware, never by the collector worker.
 */
export function validateServerEnvironment() {
  validateEnvironment(process.env, {
    VITE_APP_PROFILE: import.meta.env["VITE_APP_PROFILE"],
    VITE_ALLOW_HOSTED_SUPABASE: import.meta.env["VITE_ALLOW_HOSTED_SUPABASE"],
    VITE_SUPABASE_URL: import.meta.env["VITE_SUPABASE_URL"],
    VITE_SUPABASE_PUBLISHABLE_KEY: import.meta.env["VITE_SUPABASE_PUBLISHABLE_KEY"],
  });
}

/**
 * Server-only Supabase validation for backend processes without a browser bundle
 * (including the collector worker). It never compares against `import.meta.env`, so
 * a built artifact stays configurable from the host's runtime `process.env` alone.
 */
export function validateServerSupabaseEnvironment(
  env: Record<string, string | undefined> = process.env,
) {
  validatePublicSecrets(env);
  validateServerSupabase(env);
}
