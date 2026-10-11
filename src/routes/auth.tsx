import { createFileRoute, useNavigate, Link } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { Orbit } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { supabase } from "@/integrations/supabase/client";
import { createNonce, loadGoogleScript } from "@/lib/google-identity";

export const Route = createFileRoute("/auth")({
  ssr: false,
  head: () => ({
    meta: [
      { title: "Sign in — Crypto Watch" },
      {
        name: "description",
        content:
          "Sign in to Crypto Watch to track your crypto watchlist, price alerts and trading notes.",
      },
      { property: "og:title", content: "Sign in — Crypto Watch" },
      {
        property: "og:description",
        content: "Private crypto price monitoring, alerts and notes.",
      },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: AuthPage,
});

function AuthPage() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const googleButton = useRef<HTMLDivElement>(null);
  const [googleReady, setGoogleReady] = useState(false);
  const [logoMissing, setLogoMissing] = useState(false);
  const googleClientId = import.meta.env["VITE_GOOGLE_CLIENT_ID"] as string | undefined;

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      if (data.session) navigate({ to: "/dashboard", replace: true });
    });
  }, [navigate]);

  async function signIn(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    setBusy(false);
    if (error) {
      toast.error(error.message);
      return;
    }
    navigate({ to: "/dashboard", replace: true });
  }

  async function signUp(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const { data, error } = await supabase.auth.signUp({
      email,
      password,
      options: { emailRedirectTo: window.location.origin + "/auth/callback" },
    });
    setBusy(false);
    if (error) {
      toast.error(error.message);
      return;
    }
    if (!data.session) {
      toast.success("Check your email to confirm your account, then sign in.");
      return;
    }
    navigate({ to: "/dashboard", replace: true });
  }

  // Google Identity Services signs in inside this page, so Google's consent UI never shows the
  // Supabase domain. Google gets the hashed nonce; Supabase verifies it against the raw one.
  useEffect(() => {
    const container = googleButton.current;
    if (!googleClientId || !container) return;
    let cancelled = false;

    async function setup(clientId: string, target: HTMLElement) {
      try {
        // A new nonce per initialize call, generated before any await that React could cancel.
        const nonce = await createNonce();
        await loadGoogleScript();
        if (cancelled || !window.google) return;
        window.google.accounts.id.initialize({
          client_id: clientId,
          nonce: nonce.hashed,
          use_fedcm_for_prompt: true,
          callback: async (response) => {
            const { error } = await supabase.auth.signInWithIdToken({
              provider: "google",
              token: response.credential,
              nonce: nonce.raw,
            });
            if (error) {
              toast.error(error.message);
              return;
            }
            navigate({ to: "/dashboard", replace: true });
          },
        });
        target.replaceChildren();
        window.google.accounts.id.renderButton(target, {
          type: "standard",
          theme: "outline",
          size: "large",
          text: "continue_with",
        });
        setGoogleReady(true);
      } catch (error) {
        console.error(error);
        if (!cancelled) toast.error("Google sign-in could not load. Use email instead.");
      }
    }

    void setup(googleClientId, container);
    return () => {
      // StrictMode runs effect, cleanup, effect: the first run is discarded here.
      cancelled = true;
      setGoogleReady(false);
      window.google?.accounts.id.cancel();
      container.replaceChildren();
    };
  }, [googleClientId, navigate]);

  // Our themed button drives the (hidden) GIS button, which owns the credential flow.
  function startGoogle() {
    const rendered = googleButton.current?.querySelector<HTMLElement>("div[role=button]");
    if (rendered) {
      rendered.click();
      return;
    }
    if (!window.google) {
      toast.error("Google sign-in is unavailable. Use email instead.");
      return;
    }
    window.google.accounts.id.prompt((notification) => {
      if (notification.isNotDisplayed() || notification.isSkippedMoment()) {
        toast.error("Google sign-in is unavailable. Use email instead.");
      }
    });
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="panel relative w-full max-w-md p-6">
        <Link to="/" className="mb-6 flex items-center gap-2 font-display text-xl font-semibold">
          {logoMissing ? (
            <Orbit className="size-5 text-primary" aria-hidden />
          ) : (
            <img
              src="/logo.png"
              alt="Crypto Watcher"
              width={24}
              height={24}
              className="size-6"
              onError={() => setLogoMissing(true)}
            />
          )}
          Crypto Watch
        </Link>
        <Tabs defaultValue="signin">
          <TabsList className="w-full">
            <TabsTrigger className="flex-1" value="signin">
              Sign in
            </TabsTrigger>
            <TabsTrigger className="flex-1" value="signup">
              Create account
            </TabsTrigger>
          </TabsList>

          {(["signin", "signup"] as const).map((tab) => (
            <TabsContent key={tab} value={tab}>
              <form onSubmit={tab === "signin" ? signIn : signUp} className="space-y-4 pt-4">
                <div className="space-y-2">
                  <Label htmlFor={`${tab}-email`}>Email</Label>
                  <Input
                    id={`${tab}-email`}
                    type="email"
                    autoComplete="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor={`${tab}-password`}>Password</Label>
                  <Input
                    id={`${tab}-password`}
                    type="password"
                    autoComplete={tab === "signin" ? "current-password" : "new-password"}
                    required
                    minLength={8}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </div>
                <Button type="submit" className="w-full" disabled={busy}>
                  {tab === "signin" ? "Sign in" : "Create account"}
                </Button>
              </form>
            </TabsContent>
          ))}
        </Tabs>

        <div className="my-5 flex items-center gap-3 text-xs text-muted-foreground">
          <span className="h-px flex-1 bg-border" />
          or
          <span className="h-px flex-1 bg-border" />
        </div>
        {googleClientId ? (
          <>
            <Button
              type="button"
              variant="secondary"
              className="w-full gap-2"
              aria-label="Continue with Google"
              disabled={!googleReady}
              onClick={startGoogle}
            >
              <GoogleLogo />
              Continue with Google
            </Button>
            <div
              ref={googleButton}
              aria-hidden
              className="pointer-events-none absolute size-1 overflow-hidden opacity-0"
            />
          </>
        ) : (
          <p className="text-center text-xs text-muted-foreground">
            Google sign-in is not configured. Use email instead.
          </p>
        )}
      </div>
    </div>
  );
}

// Official multicolor Google "G"; do not recolor or distort.
function GoogleLogo() {
  return (
    <svg viewBox="0 0 48 48" width="18" height="18" aria-hidden="true" focusable="false">
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  );
}
