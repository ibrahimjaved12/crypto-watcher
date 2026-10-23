import { useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { Activity, Bitcoin, Globe, Loader2, Lock, TriangleAlert, type LucideIcon } from "lucide-react";
import { useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { supabase } from "@/integrations/supabase/client";
import { useMarket } from "@/lib/market/market-context";
import { cn } from "@/lib/utils";

/**
 * Access gate (#411): nobody has access by default. Nothing behind it mounts (no page content, no queries) until
 * `my_markets()` lists at least one market. This gate is the UI; server functions also
 * enforce the label through `requireMarketAccess` (#468), and the forex tables by RLS.
 */
export function AccessGate({ children }: { children: ReactNode }) {
  const { access } = useMarket();
  if (access === "granted") return <>{children}</>;
  if (access === "loading") {
    return (
      <div className="grid min-h-screen place-items-center" role="status" aria-label="Checking access">
        <span className="grid size-14 place-items-center rounded-full bg-primary/12 ring-1 ring-primary/30 motion-safe:animate-pulse">
          <Activity className="size-6 text-primary" aria-hidden />
        </span>
      </div>
    );
  }
  return <GateCard kind={access} />;
}

const MARKET_CHIPS: readonly { icon: LucideIcon; label: string }[] = [
  { icon: Bitcoin, label: "Crypto" },
  { icon: Globe, label: "Forex" },
];

function GateCard({ kind }: { kind: "none" | "error" }) {
  const { refetch } = useMarket();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [checking, setChecking] = useState(false);
  const error = kind === "error";
  const Icon = error ? TriangleAlert : Lock;

  async function check() {
    setChecking(true);
    try {
      await refetch();
    } finally {
      setChecking(false);
    }
  }

  async function signOut() {
    await queryClient.cancelQueries();
    queryClient.clear();
    await supabase.auth.signOut();
    navigate({ to: "/auth", replace: true });
  }

  return (
    <div className="grid min-h-screen place-items-center px-4 py-10">
      <section className="panel w-full rounded-2xl border border-border p-6 text-center sm:max-w-md sm:p-8" aria-live="polite">
        <span className="relative mx-auto grid size-[72px] place-items-center rounded-full">
          <span
            aria-hidden
            className="absolute inset-0 rounded-full opacity-70"
            style={{
              background: `conic-gradient(from 210deg, transparent, var(${error ? "--color-warn" : "--color-primary"}), transparent 70%)`,
            }}
          />
          <span
            className={cn(
              "relative grid size-[64px] place-items-center rounded-full bg-background ring-1",
              error ? "text-warn ring-warn/30" : "text-primary ring-primary/30",
            )}
          >
            <Icon className="size-7" aria-hidden />
          </span>
        </span>
        <h1 className="mt-5 font-display text-2xl font-semibold">{error ? "Can't check access" : "Access pending"}</h1>
        {error ? null : (
          <div className="mt-4 flex justify-center gap-2" aria-label="Markets">
            {MARKET_CHIPS.map(({ icon: ChipIcon, label }) => (
              <span
                key={label}
                className="flex items-center gap-1.5 rounded-full border border-dashed border-border px-3 py-1.5 text-sm text-muted-foreground opacity-55"
              >
                <ChipIcon className="size-4" aria-hidden />
                {label}
              </span>
            ))}
          </div>
        )}
        <p className="mt-4 text-sm text-muted-foreground">
          {error ? "Your data is safe. Try again." : "Ask the owner to switch it on."}
        </p>
        <div className="mt-6 flex flex-col gap-2 sm:flex-row sm:justify-center">
          <Button onClick={check} disabled={checking} className="h-11 w-full sm:h-9 sm:w-auto">
            {checking ? <Loader2 className="size-4 motion-safe:animate-spin" aria-hidden /> : null}
            {error ? "Try again" : "Check again"}
          </Button>
          <Button variant="ghost" onClick={signOut} className="h-11 w-full sm:h-9 sm:w-auto">
            Sign out
          </Button>
        </div>
      </section>
    </div>
  );
}
