import { createFileRoute } from "@tanstack/react-router";

import { AppShell } from "@/components/app-shell";
import { PaperAccountView } from "@/components/paper-account";
import { FxPaperAccountView } from "@/components/paper/fx-paper-account";
import { useMarket } from "@/lib/market/market-context";

export const Route = createFileRoute("/_authenticated/$market/paper")({
  head: () => ({ meta: [{ title: "Paper account — Crypto Watch" }] }),
  component: PaperPage,
});

/** One account per market (#454): the toggle picks which one is shown; the two are never summed. */
function PaperPage() {
  const { market } = useMarket();
  return <AppShell>{market === "forex" ? <FxPaperAccountView /> : <PaperAccountView />}</AppShell>;
}
