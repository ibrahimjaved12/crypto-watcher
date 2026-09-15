import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { useState } from "react";
import { Plus, RefreshCw, PlayCircle } from "lucide-react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { QuoteCard } from "@/components/market/quote-card";
import { PythonAnalysisPanel } from "@/components/market/python-analysis";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { addSymbol, fetchRuns, fetchWatchlist, removeSymbol } from "@/lib/db";
import { getMarketSnapshot } from "@/lib/market.functions";
import { runMyMonitorCheck } from "@/lib/monitor.functions";
import { MAX_WATCHLIST_SIZE, SUPPORTED_SYMBOLS, baseAsset } from "@/lib/market/symbols";

export const Route = createFileRoute("/_authenticated/dashboard")({
  head: () => ({
    meta: [
      { title: "Dashboard — Crypto Watch" },
      {
        name: "description",
        content: "Live prices, 5m to 24h changes and charts for the pairs on your watchlist.",
      },
      { property: "og:title", content: "Dashboard — Crypto Watch" },
      { property: "og:description", content: "Your live crypto watchlist and price changes." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Dashboard,
});

function Dashboard() {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState("");
  const runCheck = useServerFn(runMyMonitorCheck);
  const loadMarket = useServerFn(getMarketSnapshot);

  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: fetchWatchlist });
  const symbols = (watchlist.data ?? []).map((w) => w.symbol);

  const market = useQuery({
    queryKey: ["market", symbols],
    queryFn: () => loadMarket({ data: { symbols } }),
    enabled: symbols.length > 0,
    refetchInterval: 60_000,
  });

  const runs = useQuery({ queryKey: ["runs"], queryFn: fetchRuns });
  const lastRun = runs.data?.[0];

  const add = useMutation({
    mutationFn: addSymbol,
    onSuccess: () => {
      setPending("");
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: removeSymbol,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
    onError: (e: Error) => toast.error(e.message),
  });

  const check = useMutation({
    mutationFn: () => runCheck({ data: undefined }),
    onSuccess: (result) => {
      toast.success(
        `Check ${result.status}: ${result.symbolsChecked} pairs, ${result.alertsCreated} alert(s).`,
      );
      if (result.error) toast.warning(result.error);
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const available = SUPPORTED_SYMBOLS.filter((s) => !symbols.includes(s));
  const quotes = market.data?.quotes ?? [];
  const sourcesDown = quotes.length > 0 && quotes.every((q) => !q.ok);

  return (
    <AppShell>
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Market dashboard</h1>
          <p className="text-sm text-muted-foreground">
            {symbols.length}/{MAX_WATCHLIST_SIZE} pairs · prices refresh every minute while this
            page is open.
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Select value={pending} onValueChange={(v) => add.mutate(v)}>
            <SelectTrigger className="w-[180px]" disabled={available.length === 0}>
              <SelectValue placeholder="Add a pair" />
            </SelectTrigger>
            <SelectContent>
              {available.map((s) => (
                <SelectItem key={s} value={s}>
                  {baseAsset(s)}/USDT
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button variant="secondary" onClick={() => market.refetch()} disabled={market.isFetching}>
            <RefreshCw
              className={`size-4 ${market.isFetching ? "animate-spin" : ""}`}
              aria-hidden
            />
            Refresh
          </Button>
          <Button onClick={() => check.mutate()} disabled={check.isPending}>
            <PlayCircle className="size-4" aria-hidden />
            Run check now
          </Button>
        </div>
      </div>

      <div className="panel mt-5 flex flex-wrap items-center gap-x-6 gap-y-2 p-4 text-sm">
        <span className="flex items-center gap-2">
          <span className="text-muted-foreground">Data source:</span>
          <Badge variant={sourcesDown ? "destructive" : "secondary"} className="num">
            {sourcesDown
              ? "No exchange reachable"
              : (quotes.find((q) => q.source)?.source ?? "Waiting…")}
          </Badge>
        </span>
        <span className="text-muted-foreground">
          Last price update:{" "}
          <span className="num text-foreground">
            {market.data ? new Date(market.data.fetchedAt).toLocaleTimeString() : "—"}
          </span>
        </span>
        <span className="text-muted-foreground">
          Last scheduled run:{" "}
          <span className="num text-foreground">
            {lastRun
              ? `${new Date(lastRun.ran_at).toLocaleString()} (${lastRun.status})`
              : "none yet"}
          </span>
        </span>
      </div>

      <PythonAnalysisPanel symbols={symbols} />

      {sourcesDown ? (
        <p className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-4 text-sm">
          Public exchange data is not reachable from the server right now. No prices are shown and
          no values are simulated. Details are recorded with each monitoring run.
        </p>
      ) : null}

      <div className="mt-5 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {watchlist.isLoading || (market.isLoading && symbols.length > 0)
          ? symbols.map((s) => (
              <div key={s} className="panel h-64 animate-pulse p-5">
                <span className="num text-sm text-muted-foreground">{s}</span>
              </div>
            ))
          : quotes.map((quote) => (
              <QuoteCard
                key={quote.symbol}
                quote={quote}
                onRemove={() => {
                  const item = watchlist.data?.find((w) => w.symbol === quote.symbol);
                  if (item) remove.mutate(item.id);
                }}
              />
            ))}
      </div>

      {symbols.length === 0 && !watchlist.isLoading ? (
        <p className="panel mt-4 flex items-center gap-2 p-6 text-sm text-muted-foreground">
          <Plus className="size-4" aria-hidden /> Add a trading pair to start monitoring.
        </p>
      ) : null}
    </AppShell>
  );
}
