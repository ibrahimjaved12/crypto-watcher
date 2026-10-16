import { createFileRoute } from "@tanstack/react-router";

import { AppShell } from "@/components/app-shell";
import { PaperAccountView, SimulatedBanner } from "@/components/paper-account";
import { PageHeader } from "@/components/plain";

export const Route = createFileRoute("/_authenticated/paper")({
  head: () => ({ meta: [{ title: "Paper account — Crypto Watch" }] }),
  component: PaperPage,
});

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
