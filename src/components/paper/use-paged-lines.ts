import { useInfiniteQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import type { FxTradeFacts } from "@/lib/paper/fx-paper";
import type { LedgerRow } from "@/lib/paper/paper-account";
import type { Page } from "@/lib/paper/paper-data";
import { paperKeys, type PaperListKind, type PaperMarket } from "@/lib/paper/paper-view";

export type PageLoader = (args: {
  data: { market: PaperMarket; accountId: string; cursor: string | null };
}) => Promise<unknown>;

/**
 * Keyset-paginated ledger list of one market's account. The key is (market, kind, account, last boundary), so the
 * two markets never share a cache entry and a new hour refetches only the first page.
 */
export function usePagedLines(
  market: PaperMarket,
  kind: PaperListKind,
  load: PageLoader,
  accountId: string | null,
  boundary: number | null,
) {
  const query = useInfiniteQuery({
    queryKey: paperKeys.list(market, kind, accountId, boundary),
    enabled: accountId !== null,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) =>
      (await load({ data: { market, accountId: accountId ?? "", cursor: pageParam } })) as unknown as Page,
    getNextPageParam: (last) => last.nextCursor,
  });
  const pages = useMemo(() => query.data?.pages ?? [], [query.data]);
  const lines = useMemo(() => {
    const bySeq = new Map<number, LedgerRow>();
    for (const page of pages) for (const line of page.lines) bySeq.set(line.seq, line);
    return [...bySeq.values()];
  }, [pages]);
  const strategies = useMemo(() => {
    const map = new Map<string, string>();
    for (const page of pages)
      for (const [setup, strategy] of Object.entries(page.strategies)) map.set(setup, strategy);
    return map;
  }, [pages]);
  const facts = useMemo(() => {
    const out: Record<string, FxTradeFacts> = {};
    for (const page of pages) Object.assign(out, page.facts ?? {});
    return out;
  }, [pages]);
  return { query, lines, strategies, facts };
}
