import { Navigate, Outlet, createFileRoute, notFound, redirect } from "@tanstack/react-router";

import { AccessGate } from "@/components/access-gate";
import { isMarket, parseMarket, type Market } from "@/lib/market/instruments";
import { MarketProvider, useMarket } from "@/lib/market/market-context";
import { readStoredMarket } from "@/lib/market/market-storage";

// Bookmarks from before the market was in the URL (#467): /dashboard and friends open in the last used market.
const LEGACY_TARGETS = {
  dashboard: "/$market/dashboard",
  forward: "/$market/forward",
  paper: "/$market/paper",
  alerts: "/$market/alerts",
  notes: "/$market/notes",
  settings: "/$market/settings",
} as const;

function isLegacyPage(value: string): value is keyof typeof LEGACY_TARGETS {
  return Object.hasOwn(LEGACY_TARGETS, value);
}

/** The market is the first URL segment (#467): the URL is its single source of truth. */
export const Route = createFileRoute("/_authenticated/$market")({
  beforeLoad: ({ params }) => {
    if (isMarket(params.market)) return;
    if (isLegacyPage(params.market)) {
      throw redirect({ to: LEGACY_TARGETS[params.market], params: { market: readStoredMarket() }, replace: true });
    }
    throw notFound();
  },
  component: MarketLayout,
});

function MarketLayout() {
  const market = parseMarket(Route.useParams().market);
  return (
    <MarketProvider market={market}>
      <AccessGate>
        <EntitledOutlet market={market} />
      </AccessGate>
    </MarketProvider>
  );
}

/** AccessGate mounts this only once access is granted, so `entitled` is non-empty; a closed market never renders. */
function EntitledOutlet({ market }: { market: Market }) {
  const { entitled } = useMarket();
  if (!entitled.includes(market)) return <Navigate to="/$market" params={{ market: entitled[0]! }} replace />;
  return <Outlet />;
}
