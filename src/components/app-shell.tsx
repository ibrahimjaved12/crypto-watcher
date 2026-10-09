import { Link, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Bell,
  FlaskConical,
  LineChart,
  LogOut,
  NotebookPen,
  Settings2,
} from "lucide-react";
import { useCallback, useSyncExternalStore, type ReactNode } from "react";
import { humanizeReason } from "@/lib/labels";

import { Button } from "@/components/ui/button";
import { supabase } from "@/integrations/supabase/client";

const NAV = [
  { to: "/dashboard", label: "Market", icon: LineChart },
  { to: "/forward", label: "Strategy Lab", icon: FlaskConical },
  { to: "/alerts", label: "Alerts", icon: Bell },
  { to: "/notes", label: "Notes", icon: NotebookPen },
  { to: "/settings", label: "Control room", icon: Settings2 },
] as const;

export function AppShell({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Subscribe to existing cache data only: this creates no query or polling.
  const cache = queryClient.getQueryCache();
  const subscribe = useCallback((onChange: () => void) => cache.subscribe(onChange), [cache]);
  const snapshot = useCallback(
    () =>
      queryClient.getQueryData<{
        runs: { status: string; ran_at: string }[];
      }>(["operational-state"]),
    [queryClient],
  );
  const operational = useSyncExternalStore(subscribe, snapshot, () => undefined);
  const lastRun = operational?.runs[0];
  const health = lastRun ? humanizeReason(lastRun.status) : undefined;

  async function signOut() {
    await queryClient.cancelQueries();
    queryClient.clear();
    await supabase.auth.signOut();
    navigate({ to: "/auth", replace: true });
  }

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-border/70 bg-background/95">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
          <Link
            to="/dashboard"
            className="flex items-center gap-2 font-display text-lg font-semibold"
          >
            <Activity className="size-5 text-primary" aria-hidden />
            Crypto Watch
          </Link>
          {health && lastRun && (
            <span
              className="ml-auto flex items-center gap-2 text-xs text-muted-foreground"
              title={`Last monitoring check: ${new Date(lastRun.ran_at).toLocaleString()}`}
            >
              <span
                aria-hidden
                className={`size-2 rounded-full ${health.level === "ok" ? "bg-bull" : health.level === "problem" ? "bg-bear" : "bg-warn"}`}
              />
              Last check: {health.short.toLowerCase()}
            </span>
          )}
          <nav
            aria-label="Main navigation"
            className="flex w-full min-w-0 items-center gap-1 overflow-x-auto pb-1 lg:ml-auto lg:w-auto lg:pb-0"
          >
            {NAV.map(({ to, label, icon: Icon }) => (
              <Link
                key={to}
                to={to}
                title={label}
                className="flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground [&.active]:bg-accent [&.active]:text-foreground"
              >
                <Icon className="size-4" aria-hidden />
                <span>{label}</span>
              </Link>
            ))}
            <Button variant="ghost" size="sm" onClick={signOut} aria-label="Sign out">
              <LogOut className="size-4" aria-hidden />
            </Button>
          </nav>
        </div>
      </header>
      <main className="mx-auto min-w-0 max-w-6xl px-4 py-6 sm:py-8">{children}</main>
    </div>
  );
}
