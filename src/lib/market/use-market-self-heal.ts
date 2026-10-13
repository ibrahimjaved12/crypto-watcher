import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { automaticQueryOptions } from "../activity-controls";
import { getMarketSnapshot, type MarketSnapshot } from "../market.functions";
import { healDelayMs, isUnavailable, mergeHealed } from "./self-heal";

export const marketQueryKey = (symbols: readonly string[]) => ["market", symbols] as const;

export function useMarketSelfHeal(
  symbols: readonly string[],
  marketKey: ReturnType<typeof marketQueryKey>,
) {
  const automatic = automaticQueryOptions(import.meta.env["VITE_MARKET_AUTO_REFRESH_ENABLED"]);
  const queryClient = useQueryClient();
  const load = useServerFn(getMarketSnapshot);
  // Observe the dashboard cache without starting a second full-watchlist fetch.
  const market = useQuery<MarketSnapshot>({ queryKey: marketKey, enabled: false });
  const down = [
    ...new Set(
      (market.data?.quotes ?? [])
        .filter((quote) => symbols.includes(quote.symbol) && isUnavailable(quote))
        .map((quote) => quote.symbol),
    ),
  ].sort();
  const enabled = down.length > 0 && automatic.enabled && !!market.data;
  const heal = useQuery({
    queryKey: ["market-heal", down.join(",")],
    enabled,
    retry: false,
    queryFn: async () => {
      const healed = await load({ data: { symbols: down } });
      queryClient.setQueryData<MarketSnapshot>(marketKey, (old) => mergeHealed(old, healed));
      return healed;
    },
    refetchInterval: (query) => healDelayMs(query.state.dataUpdateCount),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    staleTime: 0,
    gcTime: 0,
  });
  const { refetch } = heal;
  useEffect(() => {
    if (!enabled) return;
    const resume = () => {
      if (document.visibilityState === "visible") {
        // Joining an active request prevents focus and reconnect events from duplicating a batch.
        void refetch({ cancelRefetch: false });
      }
    };
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("online", resume);
    return () => {
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("online", resume);
    };
  }, [enabled, refetch]);
  return { retrying: down.length > 0, down };
}
