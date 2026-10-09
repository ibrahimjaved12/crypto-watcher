import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { useEffect, useState } from "react";
import { automaticQueryOptions, logActivity } from "@/lib/activity-controls";
import { Plus, RefreshCw, PlayCircle } from "lucide-react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { QuoteCard } from "@/components/market/quote-card";
import { MarketRead } from "@/components/market/market-read";
import { TechnicalAnalysis } from "@/components/market/technical-analysis";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Hint } from "@/components/hint";
import { EmptyState, PageHeader, StatusChip, TimeAgo } from "@/components/plain";
import { humanizeReason, relativeTime, sourceLabel, summarizeReasons, type Level } from "@/lib/labels";
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
        `Alert check ${humanizeReason(result.status).short.toLowerCase()}: ${result.symbolsChecked} pairs checked, ${result.alertsCreated} new alert(s).`,
      );
      if (result.error) toast.warning(summarizeReasons(result.error).items.join("; "));
      queryClient.invalidateQueries({ queryKey: ["operational-state"] });
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["ta"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const available = SUPPORTED_SYMBOLS.filter((s) => !symbols.includes(s));
  const quotes = market.data?.quotes ?? [];
  const sourcesDown = quotes.length > 0 && quotes.every((q) => !q.ok);
  const source = quotes.find((q) => q.source)?.source ?? null;
  const runStatus = lastRun ? humanizeReason(lastRun.status) : null;
  const stripLevel: Level = sourcesDown
    ? "problem"
    : runStatus?.level === "problem"
      ? "problem"
      : !market.data || runStatus?.level === "wait"
        ? "wait"
        : "ok";
  const stripDetails = [
    `Data source: ${sourcesDown ? "no exchange reachable" : source ? sourceLabel(source) : "waiting for the first price load"}`,
    `Last price update: ${market.data ? new Date(market.data.fetchedAt).toLocaleString() : "not loaded yet"}`,
    `Last monitoring check: ${lastRun ? `${new Date(lastRun.ran_at).toLocaleString()} (${runStatus!.short})` : "none yet"}`,
  ].join("\n");

  return (
    <AppShell>
      <PageHeader
        eyebrow="Live market"
        title="Market"
        subtitle={
          <>
            Your watched futures pairs, their recent moves and a plain-language market read.{" "}
            <span className="num">
              {symbols.length}/{MAX_WATCHLIST_SIZE}
            </span>{" "}
            pairs ·{" "}
            {automatic.enabled
              ? "prices refresh every minute while this page is open."
              : "automatic refresh is paused; press Refresh."}
          </>
        }
        actions={
          <>
            <Select value={pending} onValueChange={(v) => add.mutate(v)}>
              <SelectTrigger className="h-9 w-[160px]" disabled={available.length === 0} aria-label="Add a pair">
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
                  ? "Turn on monitoring and market-data collection in the Control room"
                  : "Check every watched pair against your alert rule right now"
              }
            >
              <PlayCircle className="size-4" aria-hidden />
              {monitoringPaused ? "Monitoring paused" : "Check for alerts now"}
            </Button>
          </>
        }
      />

      <div
        className="panel mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5 text-sm"
        title={stripDetails}
      >
        <StatusChip level={stripLevel}>
          {stripLevel === "ok" ? "Data flowing" : stripLevel === "wait" ? "Waiting" : "Data problem"}
        </StatusChip>
        <span className="text-muted-foreground">
          {sourcesDown ? (
            "No exchange reachable"
          ) : (
            <>
              Prices from <span className="text-foreground">{source ? sourceLabel(source) : "…"}</span>
            </>
          )}
          {" · "}updated{" "}
          <span className="num text-foreground">
            {market.data ? new Date(market.data.fetchedAt).toLocaleTimeString() : "—"}
          </span>
          {" · "}last check{" "}
          {lastRun ? (
            <>
              <TimeAgo at={lastRun.ran_at} text={relativeTime(lastRun.ran_at)} />{" "}
              <span className="text-foreground">({runStatus!.short.toLowerCase()})</span>
            </>
          ) : (
            "none yet"
          )}
        </span>
        <Hint text={stripDetails}>
          <span className="text-xs">Details</span>
        </Hint>
      </div>

      {sourcesDown ? (
        <p className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-4 text-sm">
          Public exchange data is not reachable from the server right now. No prices are shown and
          no values are simulated. Details are recorded with each monitoring check.
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
        <EmptyState
          className="mt-4"
          icon={Plus}
          title="No pairs watched yet"
          body="Add a pair with “Add a pair” above. Its price, recent moves and 24h chart appear here within a minute."
        />
      ) : null}

      <MarketRead symbols={symbols} />
      <TechnicalAnalysis />
    </AppShell>
  );
}
