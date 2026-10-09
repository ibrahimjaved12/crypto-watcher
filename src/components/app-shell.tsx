import { Link, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { Bell, FlaskConical, LineChart, LogOut, NotebookPen, Orbit, SlidersHorizontal } from "lucide-react";
import { useSyncExternalStore, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { TooltipProvider } from "@/components/ui/tooltip";
import { supabase } from "@/integrations/supabase/client";
import type { getForwardDashboard } from "@/lib/forward.functions";
import { explainSignalEngine } from "@/lib/forward/forward-status";
import type { Level } from "@/lib/labels";
import type { getOperationalState } from "@/lib/operational.functions";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/dashboard", label: "Market", icon: LineChart },
  { to: "/forward", label: "Strategy Lab", icon: FlaskConical },
  { to: "/alerts", label: "Alerts", icon: Bell },
  { to: "/notes", label: "Notes", icon: NotebookPen },
  { to: "/settings", label: "Control room", icon: SlidersHorizontal },
] as const;

type OperationalState = Awaited<ReturnType<typeof getOperationalState>>;
type ForwardDashboard = Awaited<ReturnType<typeof getForwardDashboard>>;

const HEALTH: Record<Level, { dot: string; text: string }> = {
  ok: { dot: "bg-bull shadow-[0_0_8px_var(--color-bull)]", text: "Systems normal" },
  wait: { dot: "bg-warn shadow-[0_0_8px_var(--color-warn)]", text: "Some data delayed" },
  problem: { dot: "bg-bear shadow-[0_0_8px_var(--color-bear)]", text: "Needs attention" },
};

/**
 * Passive read of data a page has already loaded. Subscribes to the query cache without creating
 * a query observer, so it never fetches, never polls and never changes the page's query options.
 */
function useCachedQueryData<T>(key: readonly unknown[]): T | undefined {
  const client = useQueryClient();
  return useSyncExternalStore(
    (notify) => client.getQueryCache().subscribe(notify),
    () => client.getQueryData<T>(key),
    () => undefined,
  );
}

const OPERATIONAL_KEY = ["operational-state"] as const;
const FORWARD_KEY = ["forward-dashboard"] as const;

/** Header health dot, fed only by cached page data. Hidden until some page has loaded it. */
function SystemHealth() {
  const operational = { data: useCachedQueryData<OperationalState>(OPERATIONAL_KEY) };
  const forward = { data: useCachedQueryData<ForwardDashboard>(FORWARD_KEY) };
  if (!operational.data && !forward.data) return null;

  const reasons: { level: Level; text: string }[] = [];
  const run = operational.data?.runs[0];
  if (run?.status === "failed") reasons.push({ level: "problem", text: "Last monitoring check failed" });
  if (run?.status === "partial") reasons.push({ level: "wait", text: "Last monitoring check partly completed" });
  for (const row of operational.data?.collectorHealth ?? []) {
    if (row.status === "UNAVAILABLE")
      reasons.push({ level: "problem", text: `${row.symbol} data feed offline` });
    else if (row.status !== "LIVE") reasons.push({ level: "wait", text: `${row.symbol} data feed lagging` });
  }
  if (forward.data) {
    const latest = forward.data.latestRun as Parameters<typeof explainSignalEngine>[0];
    const line = explainSignalEngine(latest, Date.now());
    if (line.level !== "ok")
      reasons.push({
        level: line.level,
        text:
          line.level === "problem"
            ? "Strategy Lab signal engine needs attention"
            : "Strategy Lab signal engine is waiting",
      });
  }
  const level: Level = reasons.some((r) => r.level === "problem")
    ? "problem"
    : reasons.length
      ? "wait"
      : "ok";
  const unique = [...new Set(reasons.map((r) => r.text))];
  const title = unique.length ? unique.slice(0, 6).join(" · ") : "Everything loaded on this visit looks healthy";
  return (
    <Link
      to="/settings"
      title={title}
      className="flex min-h-9 items-center gap-2 rounded-full border border-border/70 px-3 text-xs text-muted-foreground hover:text-foreground"
    >
      <span className={cn("size-2 rounded-full", HEALTH[level].dot)} aria-hidden />
      <span className="hidden md:inline">{HEALTH[level].text}</span>
      <span className="sr-only md:hidden">{HEALTH[level].text}</span>
    </Link>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  async function signOut() {
    await queryClient.cancelQueries();
    queryClient.clear();
    await supabase.auth.signOut();
    navigate({ to: "/auth", replace: true });
  }

  return (
    <TooltipProvider delayDuration={150}>
      <div className="min-h-screen">
        <header className="sticky top-0 z-30 border-b border-border/60 bg-background/75 backdrop-blur-md">
          <div className="mx-auto flex max-w-6xl items-center gap-2 px-4 py-2.5">
            <Link
              to="/dashboard"
              className="flex shrink-0 items-center gap-2 font-display text-lg font-semibold"
            >
              <span className="grid size-8 place-items-center rounded-full bg-gradient-to-br from-primary/40 to-aurora/30 ring-1 ring-primary/40">
                <Orbit className="size-4 text-foreground" aria-hidden />
              </span>
              <span className="hidden sm:inline">Crypto Watch</span>
            </Link>
            <SystemHealth />
            <nav className="ml-auto flex min-w-0 items-center gap-0.5 overflow-x-auto" aria-label="Main">
              {NAV.map(({ to, label, icon: Icon }) => (
                <Link
                  key={to}
                  to={to}
                  title={label}
                  className="flex min-h-9 items-center gap-1.5 rounded-md px-2.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground [&.active]:bg-accent [&.active]:text-foreground [&.active]:shadow-[inset_0_-2px_0_var(--color-primary)]"
                >
                  <Icon className="size-4" aria-hidden />
                  <span className="hidden lg:inline">{label}</span>
                  <span className="sr-only lg:hidden">{label}</span>
                </Link>
              ))}
              <Button variant="ghost" size="icon" onClick={signOut} aria-label="Sign out" title="Sign out">
                <LogOut className="size-4" aria-hidden />
              </Button>
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
      </div>
    </TooltipProvider>
  );
}
