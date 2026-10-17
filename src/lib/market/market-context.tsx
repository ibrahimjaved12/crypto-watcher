import { useQuery } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { supabase } from "@/integrations/supabase/client";
import { MARKETS, type Market } from "./instruments";
import {
  MARKET_STORAGE_KEY,
  accessState,
  canOpen as canOpenFor,
  normalizeEntitled,
  resolveMarket,
  type AccessState,
} from "./market-select";

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

/** Market switch and access (#411): nobody has access by default; `my_markets()` lists what may be opened. */
export function MarketProvider({ children }: { children: ReactNode }) {
  const query = useQuery({ queryKey: ["my-markets"], queryFn: loadMyMarkets, staleTime: 5 * 60_000, retry: 1 });
  const ready = query.isSuccess;
  const entitled = ready ? query.data : NONE;
  const access = accessState(ready ? "ready" : query.isError ? "error" : "loading", entitled);
  const { refetch } = query;
  // While my_markets() is still loading, honour the stored choice (no crypto flash for Forex users; UI-only, RLS
  // guards the data). Once it answers or fails, a choice that is not entitled snaps to an open market. Clicks always
  // use the real `entitled`, and this fallback never writes the stored value.
  const open: readonly Market[] = ready || query.isError ? entitled : MARKETS;
  const [market, setMarketState] = useState<Market>(() => resolveMarket(readStored(), open));

  // Re-resolve on every entitlement change: a revoked plan snaps back to crypto, a granted one restores the choice.
  useEffect(() => {
    setMarketState(resolveMarket(readStored(), open));
  }, [open]);

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
    return () => {
      delete document.documentElement.dataset["market"];
    };
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
