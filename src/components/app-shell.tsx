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
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { supabase } from "@/integrations/supabase/client";

const NAV = [
  { to: "/dashboard", label: "Overview", icon: LineChart },
  { to: "/forward", label: "Strategy Lab", icon: FlaskConical },
  { to: "/alerts", label: "Alerts", icon: Bell },
  { to: "/notes", label: "Notes", icon: NotebookPen },
  { to: "/settings", label: "Settings", icon: Settings2 },
] as const;

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
    <div className="min-h-screen">
      <a href="#main-content" className="sr-only focus:not-sr-only focus:block focus:p-3">
        Skip to content
      </a>
      <header className="sticky top-0 z-30 border-b border-border/70 bg-background/80 backdrop-blur">
        <div className="mx-auto flex max-w-[1480px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 sm:px-6">
          <Link
            to="/dashboard"
            className="flex items-center gap-2 font-display text-lg font-semibold"
          >
            <Activity className="size-5 text-primary" aria-hidden />
            Crypto Watch
          </Link>
          <nav
            aria-label="Main navigation"
            className="flex w-full items-center gap-1 overflow-x-auto sm:ml-auto sm:w-auto"
          >
            {NAV.map(({ to, label, icon: Icon }) => (
              <Link
                key={to}
                aria-label={label}
                to={to}
                className="flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground [&.active]:bg-primary/10 [&.active]:text-primary [&.active]:shadow-[inset_0_-2px_0_var(--primary)]"
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
      <main id="main-content" className="mx-auto max-w-[1480px] px-4 py-8 sm:px-6 lg:py-10">
        {children}
      </main>
    </div>
  );
}
