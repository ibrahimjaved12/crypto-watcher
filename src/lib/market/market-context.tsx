import { useQuery } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { supabase } from "@/integrations/supabase/client";
import type { Market } from "./instruments";
import { MARKET_STORAGE_KEY, canOpen as canOpenFor, normalizeEntitled, resolveMarket } from "./market-select";

type MarketState = {
  market: Market;
  setMarket: (next: Market) => boolean;
  entitled: readonly Market[];
  ready: boolean;
  canOpen: (market: Market) => boolean;
};

const CRYPTO_ONLY: readonly Market[] = Object.freeze(["crypto"]);
const MarketContext = createContext<MarketState | null>(null);

function readStored(): string | null {
  try {
    return window.localStorage.getItem(MARKET_STORAGE_KEY);
  } catch {
    return null; // private windows and blocked storage
  }
}

function writeStored(market: Market) {
  try {
    window.localStorage.setItem(MARKET_STORAGE_KEY, market);
  } catch {
    // the choice still applies for this tab
  }
}

async function loadMyMarkets(): Promise<Market[]> {
  const { data, error } = await supabase.rpc("my_markets");
  if (error) throw error;
  return normalizeEntitled(data);
}

/** UI-only market switch. Never blocks rendering: until `my_markets()` answers, only crypto is open. */
export function MarketProvider({ children }: { children: ReactNode }) {
  const query = useQuery({ queryKey: ["my-markets"], queryFn: loadMyMarkets, staleTime: 5 * 60_000, retry: 1 });
  const ready = query.isSuccess;
  const entitled = ready ? query.data : CRYPTO_ONLY;
  const [market, setMarketState] = useState<Market>(() => resolveMarket(readStored(), entitled));

  // Re-resolve on every entitlement change: a revoked plan snaps back to crypto, a granted one restores the choice.
  useEffect(() => {
    setMarketState(resolveMarket(readStored(), entitled));
  }, [entitled]);

  // Two tabs agree on the market.
  useEffect(() => {
    function onStorage(event: StorageEvent) {
      if (event.key === MARKET_STORAGE_KEY) setMarketState(resolveMarket(event.newValue, entitled));
    }
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [entitled]);

  // Portals (popovers, toasts) live outside the shell, so the accent is set on <html> too.
  useEffect(() => {
    document.documentElement.dataset["market"] = market;
  }, [market]);

  const setMarket = useCallback(
    (next: Market) => {
      if (!canOpenFor(next, entitled)) return false;
      writeStored(next);
      setMarketState(next);
      return true;
    },
    [entitled],
  );
  const canOpen = useCallback((value: Market) => canOpenFor(value, entitled), [entitled]);
  const value = useMemo(
    () => ({ market, setMarket, entitled, ready, canOpen }),
    [market, setMarket, entitled, ready, canOpen],
  );
  return <MarketContext.Provider value={value}>{children}</MarketContext.Provider>;
}

export function useMarket(): MarketState {
  const value = useContext(MarketContext);
  if (!value) throw new Error("useMarket must be used inside MarketProvider");
  return value;
}
