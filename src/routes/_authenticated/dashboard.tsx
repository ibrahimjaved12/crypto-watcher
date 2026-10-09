import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { useEffect, useState } from "react";
import { automaticQueryOptions, logActivity } from "@/lib/activity-controls";
import { Plus, RefreshCw, PlayCircle } from "lucide-react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { QuoteCard } from "@/components/market/quote-card";
import { PythonAnalysisPanel } from "@/components/market/python-analysis";
import { TechnicalAnalysis } from "@/components/market/technical-analysis";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { humanizeReason } from "@/lib/labels";
import { addSymbol, fetchSettings, fetchWatchlist, removeSymbol } from "@/lib/db";
import { getMarketSnapshot } from "@/lib/market.functions";
import { runMyMonitorCheck } from "@/lib/monitor.functions";
import { getOperationalState } from "@/lib/operational.functions";
import { MAX_WATCHLIST_SIZE, SUPPORTED_SYMBOLS, baseAsset } from "@/lib/market/symbols";

export const Route = createFileRoute("/_authenticated/dashboard")({
  head: () => ({
    meta: [
      { title: "Market — Crypto Watch" },
      {
        name: "description",
        content: "Live prices, 5m to 24h changes and charts for the pairs on your watchlist.",
      },
      { property: "og:title", content: "Market — Crypto Watch" },
      { property: "og:description", content: "Your live crypto watchlist and price changes." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Dashboard,
});

function Dashboard() {
  const automatic = automaticQueryOptions(import.meta.env["VITE_MARKET_AUTO_REFRESH_ENABLED"]);
  useEffect(() => {
    if (!automatic.enabled)
      logActivity(import.meta.env["VITE_ACTIVITY_DIAGNOSTICS"], "market", "automatic-paused");
  }, [automatic.enabled]);
  const queryClient = useQueryClient();
  const [pending, setPending] = useState("");
  const runCheck = useServerFn(runMyMonitorCheck);
  const loadMarket = useServerFn(getMarketSnapshot);
  const loadOperationalState = useServerFn(getOperationalState);

  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: fetchWatchlist });
  const settings = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const symbols = (watchlist.data ?? []).map((w) => w.symbol);
  const monitoringPaused =
    settings.data !== undefined &&
    (!settings.data.monitoring_enabled || !settings.data.market_data_collection_enabled);

  const market = useQuery({
    queryKey: ["market", symbols],
    queryFn: () => {
      logActivity(import.meta.env["VITE_ACTIVITY_DIAGNOSTICS"], "market", "request-started");
      return loadMarket({ data: { symbols } });
    },
    ...automatic,
    enabled: automatic.enabled && symbols.length > 0,
  });

  const operational = useQuery({
    queryKey: ["operational-state"],
    queryFn: () => loadOperationalState(),
  });
  const lastRun = operational.data?.runs[0];

  const add = useMutation({
    mutationFn: addSymbol,
    onSuccess: () => {
      setPending("");
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    },
    onError: (e: Error) => toast.error(humanizeReason(e.message).short),
  });

  const remove = useMutation({
    mutationFn: removeSymbol,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
    onError: (e: Error) => toast.error(humanizeReason(e.message).short),
  });

  const check = useMutation({
    mutationFn: () => runCheck({ data: undefined }),
    onSuccess: (result) => {
      toast.success(
        `${humanizeReason(result.status).short}: ${result.symbolsChecked} pairs checked, ${result.alertsCreated} alerts.`,
      );
      if (result.error) toast.warning(humanizeReason(result.error).short);
      queryClient.invalidateQueries({ queryKey: ["operational-state"] });
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["ta"] });
    },
    onError: (e: Error) => toast.error(humanizeReason(e.message).short),
  });

  const available = SUPPORTED_SYMBOLS.filter((s) => !symbols.includes(s));
  const quotes = market.data?.quotes ?? [];
  const sourcesDown = quotes.length > 0 && quotes.every((q) => !q.ok);

  return (
    <AppShell>
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Market</h1>
          <p className="text-sm text-muted-foreground">
            {symbols.length}/{MAX_WATCHLIST_SIZE} pairs ·{" "}
            {automatic.enabled
              ? "prices refresh every minute while this page is active."
              : "Automatic price refresh paused. Press Refresh to load prices."}
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
          <Button
            variant="secondary"
            onClick={() => market.refetch()}
            disabled={market.isFetching || symbols.length === 0}
          >
            <RefreshCw
              className={`size-4 ${market.isFetching ? "animate-spin" : ""}`}
              aria-hidden
            />
            Refresh
          </Button>
          <Button
            onClick={() => check.mutate()}
            disabled={check.isPending || settings.isPending || settings.isError || monitoringPaused}
            title={
              monitoringPaused
                ? "Enable monitoring and market-data collection in Control room"
                : undefined
            }
          >
            <PlayCircle className="size-4" aria-hidden />
            {monitoringPaused ? "Monitoring paused" : "Run check now"}
          </Button>
        </div>
      </div>

      <details className="panel mt-5 px-4 py-1 text-xs">
        <summary className="text-muted-foreground">
          <span className="text-foreground">
            {sourcesDown
              ? "Exchange unavailable"
              : humanizeReason(quotes.find((q) => q.source)?.source ?? "Waiting for prices").short}
          </span>
          <span className="mx-2">·</span>
          Prices updated{" "}
          <span className="num">
            {market.data ? new Date(market.data.fetchedAt).toLocaleTimeString() : "not yet"}
          </span>
          <span className="mx-2">·</span>
          Last check: {lastRun ? humanizeReason(lastRun.status).short.toLowerCase() : "not run yet"}
        </summary>
        <p className="pb-2 text-muted-foreground">
          Last monitoring run: {lastRun ? new Date(lastRun.ran_at).toLocaleString() : "none yet"}.
          {lastRun
            ? ` ${humanizeReason(lastRun.status).help ?? "Open Control room for run history."}`
            : "Run a check to start recording alerts and indicator history."}
        </p>
      </details>

      {sourcesDown ? (
        <p className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-4 text-sm">
          Public exchange data is not reachable from the server right now. No prices are shown and
          no values are simulated. Details are recorded with each monitoring run.
        </p>
      ) : null}

      <div className="mt-5 grid min-w-0 gap-4 md:grid-cols-2">
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

      {symbols.length > 0 && quotes.length === 0 && !market.isFetching && !market.isLoading && (
        <p className="panel mt-4 p-5 text-sm text-muted-foreground">
          Your watched pairs are ready. Press Refresh to load prices and 24-hour charts.
        </p>
      )}
      {symbols.length === 0 && !watchlist.isLoading ? (
        <p className="panel mt-4 flex items-center gap-2 p-6 text-sm text-muted-foreground">
          <Plus className="size-4" aria-hidden /> Add a trading pair to start monitoring.
        </p>
      ) : null}
      <PythonAnalysisPanel symbols={symbols} />
      <TechnicalAnalysis />
    </AppShell>
  );
}
