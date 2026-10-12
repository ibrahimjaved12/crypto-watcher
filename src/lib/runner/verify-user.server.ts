import { createClient } from "@supabase/supabase-js";
import type { Database } from "../../integrations/supabase/types";

// Mirrors auth-middleware without importing the application server framework.
function isNewSupabaseApiKey(value: string): boolean {
  return value.startsWith('sb_publishable_') || value.startsWith('sb_secret_');
}

export function createSupabaseFetch(supabaseKey: string): typeof fetch {
  return (input, init) => {
    const headers = new Headers(
      typeof Request !== 'undefined' && input instanceof Request ? input.headers : undefined,
    );

    if (init?.headers) {
      new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    }

    // New Supabase API keys are opaque strings, not bearer JWTs.
    if (isNewSupabaseApiKey(supabaseKey) && headers.get('Authorization') === `Bearer ${supabaseKey}`) {
      headers.delete('Authorization');
    }

    headers.set('apikey', supabaseKey);
    return fetch(input, { ...init, headers });
  };
}

export class UnauthorizedError extends Error {
  readonly code = "unauthorized";
  constructor() { super("Unauthorized"); this.name = "UnauthorizedError"; }
}

export async function verifyUserToken(token: string, env: Record<string, string | undefined>) {
  try {
    if (!token || token.split(".").length !== 3) throw new UnauthorizedError();
    const url = env["SUPABASE_URL"];
    const key = env["SUPABASE_PUBLISHABLE_KEY"];
    if (!url || !key) throw new UnauthorizedError();
    const supabase = createClient<Database>(url, key, {
      global: { fetch: createSupabaseFetch(key), headers: { Authorization: `Bearer ${token}` } },
      auth: { storage: undefined, persistSession: false, autoRefreshToken: false },
    });
    const { data, error } = await supabase.auth.getClaims(token);
    if (error || !data?.claims?.sub) throw new UnauthorizedError();
    return { supabase, userId: data.claims.sub };
  } catch {
    throw new UnauthorizedError();
  }
}
