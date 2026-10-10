import { Link, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { Activity, Bell, FlaskConical, LineChart, LogOut, NotebookPen, SlidersHorizontal } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { TooltipProvider } from "@/components/ui/tooltip";
import { supabase } from "@/integrations/supabase/client";

const NAV = [
  { to: "/dashboard", label: "Market", icon: LineChart },
  { to: "/forward", label: "Strategy Lab", icon: FlaskConical },
  { to: "/alerts", label: "Alerts", icon: Bell },
  { to: "/notes", label: "Notes", icon: NotebookPen },
  { to: "/settings", label: "Control room", icon: SlidersHorizontal },
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
    <TooltipProvider delayDuration={150}>
      <div className="min-h-screen">
        <header className="sticky top-0 z-30 border-b border-border/60 bg-background/75 backdrop-blur-md">
          <div className="mx-auto flex max-w-6xl items-center gap-2 px-4 py-2.5">
            <Link
              to="/dashboard"
              className="flex shrink-0 items-center gap-2 font-display text-lg font-semibold"
            >
              <Activity className="size-5 text-primary" aria-hidden />
              Crypto Watch
            </Link>
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
