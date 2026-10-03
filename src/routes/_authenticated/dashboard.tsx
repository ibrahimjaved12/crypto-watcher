import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { useEffect, useState } from "react";
import { automaticQueryOptions, logActivity } from "@/lib/activity-controls";
import { RefreshCw, PlayCircle } from "lucide-react";
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
import { EmptyState, ErrorNotice, PageHeader, StatusBadge } from "@/components/presentation";
import {
  issueSummary,
  monitoringRunLabel,
  sourceLabel,
  pairLabel,
  statusTone,
} from "@/lib/presentation/labels";
import { addSymbol, fetchSettings, fetchWatchlist, removeSymbol } from "@/lib/db";
import { getMarketSnapshot } from "@/lib/market.functions";
import { runMyMonitorCheck } from "@/lib/monitor.functions";
import { getOperationalState } from "@/lib/operational.functions";
import { MAX_WATCHLIST_SIZE, SUPPORTED_SYMBOLS, baseAsset } from "@/lib/market/symbols";

export const Route = createFileRoute("/_authenticated/dashboard")({
  head: () => ({
    meta: [
      { title: "Market Overview — Crypto Watch" },
      {
        name: "description",
        content: "Live prices, 5m to 24h changes and charts for the pairs on your watchlist.",
      },
      { property: "og:title", content: "Market Overview — Crypto Watch" },
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
      const notify =
        result.status === "success"
          ? toast.success
          : result.status === "failed"
            ? toast.error
            : toast.warning;
      notify(
        `${monitoringRunLabel(result.status)}: ${result.symbolsChecked} pairs checked, ${result.alertsCreated} alerts created.`,
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
        description={`${symbols.length}/${MAX_WATCHLIST_SIZE} watched pairs. ${automatic.enabled ? "Prices refresh every minute while this page is active." : "Automatic price refresh is paused. Refresh prices to load a snapshot."}`}
      >
        <Select value={pending} onValueChange={(v) => add.mutate(v)}>
          <SelectTrigger
            className="w-[180px]"
            aria-label="Add pair"
            disabled={available.length === 0 || add.isPending || watchlist.isPending}
          >
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
          <RefreshCw className={`size-4 ${market.isFetching ? "animate-spin" : ""}`} aria-hidden />
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
      </PageHeader>

      <div className="panel mt-6 grid gap-4 p-4 text-sm sm:grid-cols-3">
        <div>
          <p className="mb-2 text-xs text-muted-foreground">Market data</p>
          <StatusBadge
            tone={
              sourcesDown
                ? "danger"
                : quotes.some((q) => !q.ok || q.stale)
                  ? "warning"
                  : quotes.length
                    ? "good"
                    : "neutral"
            }
          >
            {sourcesDown
              ? "Unavailable"
              : quotes.some((q) => !q.ok)
                ? "Some prices unavailable"
                : quotes.some((q) => q.stale)
                  ? "Delayed"
                  : quotes.length
                    ? "Prices loaded"
                    : "Awaiting prices"}
          </StatusBadge>
          <p className="mt-2 text-xs text-muted-foreground">
            {quotes.find((q) => q.source)?.source
              ? [...new Set(quotes.map((q) => q.source).filter(Boolean))]
                  .map(sourceLabel)
                  .join(" · ")
              : "No source received yet"}
          </p>
        </div>
        <div>
          <p className="mb-2 text-xs text-muted-foreground">Prices refreshed</p>
          <p className="num text-sm">
            {market.data ? new Date(market.data.fetchedAt).toLocaleString() : "—"}
          </p>
          <p className="mt-2 text-xs text-muted-foreground">
            {market.isFetching
              ? "Refreshing prices…"
              : "Candle freshness is shown on each market card."}
          </p>
        </div>
        <div>
          <p className="mb-2 text-xs text-muted-foreground">Last monitoring check</p>
          <StatusBadge tone={monitoringPaused ? "neutral" : statusTone(lastRun?.status)}>
            {monitoringPaused
              ? "Monitoring paused"
              : lastRun
                ? monitoringRunLabel(lastRun.status)
                : operational.isPending
                  ? "Loading status…"
                  : operational.isError
                    ? "Status unavailable"
                    : "No checks yet"}
          </StatusBadge>
          <p className="mt-2 text-xs text-muted-foreground">
            {lastRun
              ? new Date(lastRun.ran_at).toLocaleString()
              : "Check the market to begin recording results."}
          </p>
        </div>
      </div>
      {monitoringPaused && (
        <p className="mt-3 text-sm text-muted-foreground">
          Enable monitoring and market-data collection in Settings to check the market.
        </p>
      )}
      {settings.error && (
        <div className="mt-4">
          <ErrorNotice title="Monitoring preferences unavailable." error={settings.error} />
        </div>
      )}
      {add.error && (
        <div className="mt-4">
          <ErrorNotice title="Pair could not be added." error={add.error} />
        </div>
      )}
      {remove.error && (
        <div className="mt-4">
          <ErrorNotice title="Pair could not be removed." error={remove.error} />
        </div>
      )}
      {check.data?.error && (
        <div className="mt-4">
          <ErrorNotice title="Monitoring check needs attention." error={check.data.error} />
        </div>
      )}
      {watchlist.error && (
        <div className="mt-4">
          <ErrorNotice title="Watchlist unavailable." error={watchlist.error} />
        </div>
      )}
      {market.error && (
        <div className="mt-4">
          <ErrorNotice title="Prices could not be refreshed." error={market.error} />
        </div>
      )}
      {check.error && (
        <div className="mt-4">
          <ErrorNotice error={check.error} />
        </div>
      )}
      {operational.error && (
        <div className="mt-4">
          <ErrorNotice title="Monitoring status unavailable." error={operational.error} />
        </div>
      )}
      <div className="mb-4 mt-8 flex items-center justify-between">
        <h2 className="text-lg font-semibold">Watched markets</h2>
        <span className="text-xs text-muted-foreground">Price & movement</span>
      </div>

      {sourcesDown ? (
        <p className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-4 text-sm">
          Market prices are unavailable. Review the details on each market card or try Refresh
          prices.
        </p>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {watchlist.isLoading || (market.isLoading && symbols.length > 0)
          ? (symbols.length ? symbols : ["Loading watchlist"]).map((s) => (
              <div key={s} role="status" className="panel h-64 animate-pulse p-5">
                <span className="text-sm text-muted-foreground">{pairLabel(s)}</span>
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

      {symbols.length === 0 && !watchlist.isPending && !watchlist.isError ? (
        <EmptyState title="Your market watch starts here">
          Add a pair above to follow its price, movement and completed-candle analysis.
        </EmptyState>
      ) : null}
      {symbols.length > 0 && !quotes.length && !market.isFetching && !market.isError && (
        <EmptyState title="Ready to load your markets">
          Refresh prices for the latest available snapshot.
        </EmptyState>
      )}
      <PythonAnalysisPanel symbols={symbols} />
      <TechnicalAnalysis />
    </AppShell>
  );
}
