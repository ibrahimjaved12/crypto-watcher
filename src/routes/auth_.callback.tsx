import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import { toast } from "sonner";

import { supabase } from "@/integrations/supabase/client";

// Supabase redirects here after Google sign-in and after email confirmation (PKCE flow).
// The `auth_` file name keeps this route out of the `/auth` layout while serving /auth/callback.
export const Route = createFileRoute("/auth_/callback")({
  ssr: false,
  head: () => ({ meta: [{ title: "Signing in — Crypto Watch" }] }),
  component: AuthCallback,
});

function AuthCallback() {
  const navigate = useNavigate();
  // The code is single use; guard against a second run (React strict mode in dev).
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    async function finish() {
      const params = new URLSearchParams(window.location.search);
      const providerError = params.get("error_description") ?? params.get("error");
      const code = params.get("code");
      if (providerError || !code) {
        toast.error(providerError ?? "Sign-in link is missing its code. Try again.");
        navigate({ to: "/auth", replace: true });
        return;
      }
      const { error } = await supabase.auth.exchangeCodeForSession(code);
      if (error) {
        console.error(error);
        toast.error(error.message);
        navigate({ to: "/auth", replace: true });
        return;
      }
      navigate({ to: "/dashboard", replace: true });
    }

    void finish();
  }, [navigate]);

  return (
    <div className="flex min-h-screen items-center justify-center px-4 text-sm text-muted-foreground">
      Signing you in…
    </div>
  );
}
