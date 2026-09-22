import { validateEnvironment } from "./environment";

export function validateServerEnvironment() {
  validateEnvironment(process.env, {
    VITE_APP_PROFILE: import.meta.env["VITE_APP_PROFILE"],
    VITE_ALLOW_HOSTED_SUPABASE: import.meta.env["VITE_ALLOW_HOSTED_SUPABASE"],
    VITE_SUPABASE_URL: import.meta.env["VITE_SUPABASE_URL"],
    VITE_SUPABASE_PUBLISHABLE_KEY: import.meta.env["VITE_SUPABASE_PUBLISHABLE_KEY"],
  });
}
