import { PageHeader, QueryNotice, TechnicalDetails } from "@/components/presentation";
import { sourceLabel, monitoringRunLabel, issueSummary } from "@/lib/presentation/labels";
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
import { Badge } from "@/components/ui/badge";
import { addSymbol, fetchSettings, fetchWatchlist, removeSymbol } from "@/lib/db";
import { getMarketSnapshot } from "@/lib/market.functions";
import { runMyMonitorCheck } from "@/lib/monitor.functions";
import { getOperationalState } from "@/lib/operational.functions";
import { MAX_WATCHLIST_SIZE, SUPPORTED_SYMBOLS, baseAsset } from "@/lib/market/symbols";

export const Route = createFileRoute("/_authenticated/dashboard")({
  head: () => ({
    meta: [
      { title: "Overview — Crypto Watch" },
      {
        name: "description",
        content: "Live prices, 5m to 24h changes and charts for the pairs on your watchlist.",
      },
      { property: "og:title", content: "Overview — Crypto Watch" },
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
    onError: (e: Error) => toast.error(issueSummary(e.message)),
  });

  const remove = useMutation({
    mutationFn: removeSymbol,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
    onError: (e: Error) => toast.error(issueSummary(e.message)),
  });

  const check = useMutation({
    mutationFn: () => runCheck({ data: undefined }),
    onSuccess: (result) => {
      (result.status === "success" ? toast.success : toast.warning)(
        `${monitoringRunLabel(result.status)}: ${result.symbolsChecked} pairs, ${result.alertsCreated} alert(s).`,
      );
      if (result.error) toast.warning(issueSummary(result.error));
      queryClient.invalidateQueries({ queryKey: ["operational-state"] });
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["ta"] });
    },
    onError: (e: Error) => toast.error(issueSummary(e.message)),
  });

  const available = SUPPORTED_SYMBOLS.filter((s) => !symbols.includes(s));
  const quotes = market.data?.quotes ?? [];
  const sourcesDown = quotes.length > 0 && quotes.every((q) => !q.ok);

  return (
    <AppShell>
      <PageHeader
        title="Market Overview"
        description={`${symbols.length}/${MAX_WATCHLIST_SIZE} watched pairs. ${automatic.enabled ? "Prices refresh every minute while this page is active." : "Automatic refresh is paused. Refresh prices to load a snapshot."}`}
      >
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
            Refresh prices
          </Button>
          <Button
            onClick={() => check.mutate()}
            disabled={check.isPending || settings.isPending || settings.isError || monitoringPaused}
            title={
              monitoringPaused
                ? "Enable monitoring and market-data collection in Settings"
                : undefined
            }
          >
            <PlayCircle className="size-4" aria-hidden />
            {monitoringPaused
              ? "Monitoring paused"
              : check.isPending
                ? "Checking market…"
                : "Check market now"}
          </Button>
        </div>
      </PageHeader>

      <QueryNotice
        pending={watchlist.isPending}
        error={watchlist.error || market.error || operational.error || settings.error}
      />
      <div className="panel mt-5 flex flex-wrap items-center gap-x-6 gap-y-2 p-4 text-sm">
        <span className="flex items-center gap-2">
          <span className="text-muted-foreground">Data source:</span>
          <Badge variant={sourcesDown ? "destructive" : "secondary"}>
            {sourcesDown
              ? "No exchange reachable"
              : quotes.find((q) => q.source)?.source
                ? sourceLabel(quotes.find((q) => q.source)?.source)
                : "Waiting for prices"}
          </Badge>
        </span>
        <span className="text-muted-foreground">
          Last price update:{" "}
          <span className="num text-foreground">
            {market.data ? new Date(market.data.fetchedAt).toLocaleTimeString() : "—"}
          </span>
        </span>
        <span className="text-muted-foreground">
          Last monitoring check:{" "}
          <span className="text-foreground">
            {lastRun
              ? `${monitoringRunLabel(lastRun.status)} · ${new Date(lastRun.ran_at).toLocaleString()}`
              : operational.isPending
                ? "Loading…"
                : operational.error
                  ? "Unavailable"
                  : "No checks yet"}
          </span>
        </span>
      </div>

      {lastRun?.error_message && (
        <p className="mt-3 text-sm text-warn">{issueSummary(lastRun.error_message)}</p>
      )}
      {(check.error || add.error || remove.error) && (
        <div role="alert" className="mt-3 text-sm text-destructive">
          {issueSummary((check.error || add.error || remove.error)!.message)}
          <TechnicalDetails>{(check.error || add.error || remove.error)!.message}</TechnicalDetails>
        </div>
      )}
      {lastRun?.error_message && <TechnicalDetails>{lastRun.error_message}</TechnicalDetails>}
      {monitoringPaused && (
        <p className="mt-3 text-sm text-muted-foreground">
          Enable monitoring and market-data collection in Settings to check for alerts.
        </p>
      )}
      <h2 className="mt-8 text-lg font-semibold">Watched markets</h2>

      {sourcesDown ? (
        <p className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-4 text-sm">
          Public exchange data is not reachable from the server right now. No prices are shown and
          no values are simulated. Details are recorded with each monitoring run.
        </p>
      ) : null}

      <div className="mt-3 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
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

      {symbols.length === 0 && !watchlist.isPending && !watchlist.error ? (
        <p className="panel mt-4 flex items-center gap-2 p-6 text-sm text-muted-foreground">
          <Plus className="size-4" aria-hidden /> Add a trading pair to start monitoring.
        </p>
      ) : null}
      {symbols.length > 0 && !market.isFetching && !quotes.length && !market.error && (
        <p className="panel mt-4 p-5 text-sm text-muted-foreground">
          Your watchlist is ready. Refresh prices to load market cards.
        </p>
      )}
      <PythonAnalysisPanel symbols={symbols} />
      <TechnicalAnalysis />
    </AppShell>
  );
}
