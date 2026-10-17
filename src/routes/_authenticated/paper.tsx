import { createFileRoute } from "@tanstack/react-router";

import { AppShell } from "@/components/app-shell";
import { ForexLanding } from "@/components/forex-landing";
import { PaperAccountView, SimulatedBanner } from "@/components/paper-account";
import { PageHeader } from "@/components/plain";
import { useMarket } from "@/lib/market/market-context";

export const Route = createFileRoute("/_authenticated/paper")({
  head: () => ({ meta: [{ title: "Paper account — Crypto Watch" }] }),
  component: PaperPageRoute,
});

/** Forex has no data feed yet (#374): its own landing screen; the crypto page below is unchanged. */
function PaperPageRoute() {
  const { market } = useMarket();
  return market === "forex" ? (
    <AppShell>
      <ForexLanding page="paper" />
    </AppShell>
  ) : (
    <PaperPage />
  );
}

function PaperPage() {
  return (
    <AppShell>
      <div className="space-y-6">
        <PageHeader
          eyebrow="Simulated · fake money"
          title="Paper account"
          subtitle="The simulated futures wallet: what it holds, what it did, and what it was not allowed to do."
        />
        <SimulatedBanner />
        <PaperAccountView />
      </div>
    </AppShell>
  );
}
