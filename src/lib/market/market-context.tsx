import { useQuery } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from "react";

import { supabase } from "@/integrations/supabase/client";
import type { Market } from "./instruments";
import { accessState, canOpen as canOpenFor, normalizeEntitled, type AccessState } from "./market-select";
import { writeStoredMarket } from "./market-storage";

type MarketState = {
  market: Market;
  setMarket: (next: Market) => boolean;
  entitled: readonly Market[];
  ready: boolean;
  canOpen: (market: Market) => boolean;
  access: AccessState;
  refetch: () => unknown;
};

const NONE: readonly Market[] = Object.freeze([]);
const MarketContext = createContext<MarketState | null>(null);

async function loadMyMarkets(): Promise<Market[]> {
  const { data, error } = await supabase.rpc("my_markets");
  if (error) throw error;
  return normalizeEntitled(data);
}

/**
 * Market and access (#411, #467): the market comes from the URL's first segment; nobody has access by default and
 * `my_markets()` lists what may be opened. Switching navigates to the same page in the other market.
 */
export function MarketProvider({ market, children }: { market: Market; children: ReactNode }) {
  const router = useRouter();
  const query = useQuery({ queryKey: ["my-markets"], queryFn: loadMyMarkets, staleTime: 5 * 60_000, retry: 1 });
  const ready = query.isSuccess;
  const entitled = ready ? query.data : NONE;
  const access = accessState(ready ? "ready" : query.isError ? "error" : "loading", entitled);
  const { refetch } = query;

  // Remember the last market actually opened; entry redirects (sign-in, old bookmarks) start there.
  useEffect(() => {
    if (entitled.includes(market)) writeStoredMarket(market);
  }, [market, entitled]);

  // Portals (popovers, toasts) live outside the shell, so the accent is set on <html> too.
  useEffect(() => {
    document.documentElement.dataset["market"] = market;
    return () => {
      delete document.documentElement.dataset["market"];
    };
  }, [market]);

  const setMarket = useCallback(
    (next: Market) => {
      if (!canOpenFor(next, entitled)) return false;
      void router.navigate({ to: ".", params: (previous) => ({ ...previous, market: next }) });
      return true;
    },
    [router, entitled],
  );
  const canOpen = useCallback((value: Market) => canOpenFor(value, entitled), [entitled]);
  const value = useMemo(
    () => ({ market, setMarket, entitled, ready, canOpen, access, refetch }),
    [market, setMarket, entitled, ready, canOpen, access, refetch],
  );
  return <MarketContext.Provider value={value}>{children}</MarketContext.Provider>;
}

export function useMarket(): MarketState {
  const value = useContext(MarketContext);
  if (!value) throw new Error("useMarket must be used inside MarketProvider");
  return value;
}
