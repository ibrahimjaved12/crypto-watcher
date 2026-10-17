import { Link, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Bell,
  FlaskConical,
  LineChart,
  LogOut,
  NotebookPen,
  SlidersHorizontal,
  Wallet,
} from "lucide-react";
import type { ReactNode } from "react";

import { MarketToggle } from "@/components/market-toggle";
import { RunnerStatusBadge, RunnerStatusNotice } from "@/components/runner-status-notice";
import { Button } from "@/components/ui/button";
import { TooltipProvider } from "@/components/ui/tooltip";
import { supabase } from "@/integrations/supabase/client";
import { useMarket } from "@/lib/market/market-context";

const NAV = [
  { to: "/dashboard", label: "Market", short: "Market", icon: LineChart },
  { to: "/forward", label: "Strategy Lab", short: "Lab", icon: FlaskConical },
  { to: "/paper", label: "Paper account", short: "Paper", icon: Wallet },
  { to: "/alerts", label: "Alerts", short: "Alerts", icon: Bell },
  { to: "/notes", label: "Notes", short: "Notes", icon: NotebookPen },
  { to: "/settings", label: "Control room", short: "Control", icon: SlidersHorizontal },
] as const;

export function AppShell({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { market } = useMarket();

  async function signOut() {
    await queryClient.cancelQueries();
    queryClient.clear();
    await supabase.auth.signOut();
    navigate({ to: "/auth", replace: true });
  }

  return (
    <TooltipProvider delayDuration={150}>
      <div className="min-h-screen" data-market={market}>
        <header className="sticky top-0 z-30 border-b border-border/60 bg-background/75 backdrop-blur-md">
          <div className="mx-auto max-w-6xl px-3 sm:px-4">
            {/* One row on every width; a fixed height and nothing in it depends on the market, so a switch moves nothing. */}
            <div className="flex h-12 flex-nowrap items-center gap-2 sm:gap-3">
              <Link
                to="/dashboard"
                aria-label="Crypto Watch"
                className="flex shrink-0 items-center gap-2 font-display text-lg font-semibold"
              >
                {/* The logo never changes with the market: text-brand is not overridden by the forex theme. */}
                <Activity className="size-5 text-brand" aria-hidden />
                <span className="hidden lg:inline">Crypto Watch</span>
              </Link>
              <MarketToggle className="shrink-0" />
              <nav aria-label="Main" className="ml-auto hidden min-w-0 items-center gap-0.5 overflow-hidden sm:flex">
                {NAV.map(({ to, label, icon: Icon }) => (
                  <Link
                    key={to}
                    to={to}
                    title={label}
                    className="group flex min-h-10 shrink-0 items-center gap-1.5 rounded-lg px-2.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground [&.active]:bg-primary/12 [&.active]:text-foreground [&.active]:shadow-[inset_0_-2px_0_var(--color-primary)]"
                  >
                    <Icon className="size-4 shrink-0" aria-hidden />
                    <span className="hidden whitespace-nowrap group-[.active]:inline xl:inline">{label}</span>
                    <span className="sr-only group-[.active]:hidden xl:hidden">{label}</span>
                  </Link>
                ))}
              </nav>
              <div className="flex shrink-0 items-center gap-0.5 max-sm:ml-auto">
                <RunnerStatusBadge />
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-10"
                  onClick={signOut}
                  aria-label="Sign out"
                  title="Sign out"
                >
                  <LogOut className="size-4" aria-hidden />
                </Button>
              </div>
            </div>
          </div>
          <div className="market-line" aria-hidden />
        </header>
        {/* Phone navigation: a bottom tab bar within thumb reach. */}
        <nav
          aria-label="Main"
          className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-6 border-t border-border/60 bg-background/85 pb-[env(safe-area-inset-bottom)] backdrop-blur-md sm:hidden"
        >
          {NAV.map(({ to, label, short, icon: Icon }) => (
            <Link
              key={to}
              to={to}
              title={label}
              aria-label={label}
              className="group flex h-14 flex-col items-center justify-center gap-0.5 text-muted-foreground transition-colors [&.active]:text-foreground [&.active]:shadow-[inset_0_2px_0_var(--color-primary)]"
            >
              <Icon className="size-5 group-[.active]:text-primary" aria-hidden />
              <span className="text-[10px] leading-none">{short}</span>
            </Link>
          ))}
        </nav>
        <main
          key={market}
          className="mx-auto max-w-6xl px-4 pt-6 pb-24 sm:pb-6 motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-200"
        >
          <RunnerStatusNotice />
          {children}
        </main>
      </div>
    </TooltipProvider>
  );
}
