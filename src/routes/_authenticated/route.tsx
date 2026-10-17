import { createFileRoute, Outlet, redirect } from "@tanstack/react-router";

import { supabase } from "@/integrations/supabase/client";
import { AccessGate } from "@/components/access-gate";
import { MarketProvider } from "@/lib/market/market-context";

export const Route = createFileRoute("/_authenticated")({
  ssr: false,
  beforeLoad: async () => {
    const { data, error } = await supabase.auth.getUser();
    if (error || !data.user) throw redirect({ to: "/auth" });
    return { user: data.user };
  },
  component: () => (
    <MarketProvider>
      <AccessGate>
        <Outlet />
      </AccessGate>
    </MarketProvider>
  ),
});
