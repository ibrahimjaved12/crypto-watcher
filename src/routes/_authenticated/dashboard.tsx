import { createFileRoute } from "@tanstack/react-router";
import { keepPreviousData, useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { useEffect, useRef, useState } from "react";
import { automaticQueryOptions, logActivity } from "@/lib/activity-controls";
import { Plus, RefreshCw, PlayCircle, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { QuoteCard, QuoteCardSkeleton } from "@/components/market/quote-card";
import { MarketRead } from "@/components/market/market-read";
import { MarketStrip } from "@/components/market/market-strip";
import { PairAssessment, type TaFrameState } from "@/components/market/pair-assessment";
import { TechnicalAnalysis } from "@/components/market/technical-analysis";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { EmptyState, PageHeader } from "@/components/plain";
import { humanizeReason, sourceLabel } from "@/lib/labels";
import { useRunnerStatus } from "@/lib/runner/use-runner-status";
import { addSymbol, fetchSettings, fetchWatchlist, removeSymbol } from "@/lib/db";
import { getMarketSnapshot, type MarketSnapshot } from "@/lib/market.functions";
import { marketQueryKey, useMarketSelfHeal } from "@/lib/market/use-market-self-heal";
import { runMyMonitorCheck } from "@/lib/monitor.functions";
import { describeAlertCheck } from "@/lib/monitor/check-message";
import { getOperationalState } from "@/lib/operational.functions";
import { getMarketMovementCurrentState, getSlowMovementState } from "@/lib/market-movement.functions";
import { slowPairRows, slowStatus } from "@/lib/market/slow-movement-view";
import {
  PUBLISHED_COPY_OFFLINE_AFTER_MS,
  toMarketMovementCurrentState,
} from "@/lib/market/market-movement-state";
import {
  MOVEMENT_POLL_INTERVAL_MS,
  TA_ASSESSMENT_FRAMES,
  buildFreshness,
  buildMarketStrip,
  buildPairMovement,
  buildTaFrame,
  formatAge,
  gradeFreshness,
  latestTaBySymbol,
  nextLimitCrossing,
  stripStatus,
  taLimitMs,
  type FreshnessGrade,
  type FreshnessItem,
  type MovementReadStatus,
} from "@/lib/market/assessment";
import { QUOTE_STALE_MS } from "@/lib/market/quote-calc";
import { loadLatestTa } from "@/lib/market/ta-latest";
import { useNow } from "@/lib/use-now";
import { useResumeSettle } from "@/lib/use-resume-settle";
import { useStaleSelfHeal } from "@/lib/use-stale-self-heal";
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

/**
 * Quote errors and failed exchange attempts for every pair, including unavailable quotes, as Diagnostics rows.
 * Each reason is listed once, with the pairs it applied to.
 */
function quoteDetailRows(quotes: MarketSnapshot["quotes"]): Array<[string, string]> {
  const pairsByReason = new Map<string, Set<string>>();
  const addReason = (reason: string, symbol: string) => {
    const pairs = pairsByReason.get(reason) ?? new Set<string>();
    pairs.add(symbol);
    pairsByReason.set(reason, pairs);
  };
  for (const quote of quotes) {
    if (quote.error) addReason(quote.error, quote.symbol);
    for (const attempt of quote.fallbackReasons) {
      const status = attempt.status === null ? "" : `HTTP ${attempt.status} `;
      const detail = `${status}${attempt.message}`.trim() || "no details";
      const reason = `${sourceLabel(attempt.source)}: ${detail}`;
      addReason(reason, quote.symbol);
    }
  }
  return Array.from(pairsByReason, ([reason, pairs]): [string, string] => ["Price diagnostics", `${reason} (${[...pairs].join(", ")})`]);
}

type RefreshState = { refreshing: boolean; lastRefreshAt: number | null; lastAttemptFailed: boolean };
const refreshOf = (query: { isFetching: boolean; dataUpdatedAt: number; errorUpdatedAt: number; isError: boolean }): RefreshState => ({
  refreshing: query.isFetching,
  lastRefreshAt: Math.max(query.dataUpdatedAt, query.errorUpdatedAt) || null,
  lastAttemptFailed: query.isError,
});
const GRADE_ORDER: Record<FreshnessGrade, number> = { fresh: 0, catching_up: 1, stale: 2 };
const worse = (...grades: FreshnessGrade[]): FreshnessGrade =>
  grades.reduce((a, b) => (GRADE_ORDER[b] > GRADE_ORDER[a] ? b : a), "fresh");

/**
 * Arms one refetch for the moment the soonest datum crosses its limit, so it is refreshed as it crosses instead of
 * up to a whole poll interval later. Visible tabs only; nothing repeats when the refetch brings no newer data.
 */
function usePreEmptiveRefresh(
  targets: Array<{ key: string; asOf: number | null; limitMs: number; refetch: () => unknown }>,
  now: number,
  enabled: boolean,
) {
  const latest = useRef(targets);
  latest.current = targets;
  let soonest: { at: number; key: string } | null = null;
  for (const target of targets) {
    const at = nextLimitCrossing([target], now);
    if (at !== null && (soonest === null || at < soonest.at)) soonest = { at, key: target.key };
  }
  const at = soonest?.at ?? null;
  const key = soonest?.key ?? null;
  useEffect(() => {
    if (!enabled || at === null || key === null) return;
    const timer = setTimeout(() => {
      if (document.visibilityState !== "visible") return;
      void latest.current.find((target) => target.key === key)?.refetch();
    }, Math.max(0, at - Date.now()));
    return () => clearTimeout(timer);
  }, [enabled, at, key]);
}

function Dashboard() {
  const runner = useRunnerStatus();
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
  const loadMovement = useServerFn(getMarketMovementCurrentState);
  const loadSlow = useServerFn(getSlowMovementState);

  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: fetchWatchlist });
  const settings = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const symbols = (watchlist.data ?? []).map((w) => w.symbol);
  const monitoringPaused =
    settings.data !== undefined &&
    (!settings.data.monitoring_enabled || !settings.data.market_data_collection_enabled);

  const marketKey = marketQueryKey(symbols);
  const movementKey = ["market-movement"] as const;
  const taKeys = TA_ASSESSMENT_FRAMES.map((timeframe) => ["ta", timeframe, "dashboard-latest", symbols.join(",")] as const);
  // A tab that comes back after a while refetches first and holds the clock meanwhile, so no age jumps past its
  // limit in the moment before the fresh data lands. `now` is the only clock of the assessment path.
  const slowKey = ["market-movement-slow"] as const;
  const settling = useResumeSettle([marketKey, movementKey, slowKey, ...taKeys]);
  const now = useNow(15_000, { hold: settling });

  const market = useQuery({
    queryKey: marketKey,
    placeholderData: keepPreviousData,
    queryFn: () => {
      logActivity(import.meta.env["VITE_ACTIVITY_DIAGNOSTICS"], "market", "request-started");
      return loadMarket({ data: { symbols } });
    },
    ...automatic,
    enabled: automatic.enabled && symbols.length > 0,
  });
  const { retrying, down } = useMarketSelfHeal(symbols, marketKey);

  // "Current assessment" (#30): the engine's persisted state (read through the existing server function, which
  // is cheap on the Worker) and the latest saved TA snapshots. The movement state is polled every 30 s, the
  // engine's and the publisher's own cadence; prices and TA keep the shared 60 s interval.
  const taAutomatic = automaticQueryOptions(import.meta.env["VITE_TA_HISTORY_AUTO_REFRESH_ENABLED"]);
  const movement = useQuery({
    queryKey: movementKey,
    queryFn: () => loadMovement(),
    ...automatic,
    refetchInterval: automatic.enabled ? MOVEMENT_POLL_INTERVAL_MS : false,
    enabled: automatic.enabled && symbols.length > 0,
  });
  // "Longer moves" (slow-movement-v1): published once per 15-minute boundary; the shared 60 s poll is plenty.
  const slow = useQuery({
    queryKey: slowKey,
    queryFn: () => loadSlow(),
    ...automatic,
    enabled: automatic.enabled && symbols.length > 0,
  });
  const slowState = slowStatus(slow.data, now);
  const slowShown = slowState === "current" || slowState === "catching_up";
  const taQueries = useQueries({
    queries: TA_ASSESSMENT_FRAMES.map((timeframe, index) => ({
      queryKey: taKeys[index]!,
      queryFn: () => loadLatestTa(timeframe, symbols),
      ...taAutomatic,
      enabled: taAutomatic.enabled && symbols.length > 0,
    })),
  });
  const movementRead: MovementReadStatus = movement.data
    ? movement.data.readStatus
    : movement.isError
      ? "read_failed"
      : "not_loaded";
  const movementState = movement.data ?? toMarketMovementCurrentState(null, { now });
  const taRows = taQueries.flatMap((query) => query.data ?? []);
  const taLoaded = taQueries.some((query) => query.data !== undefined);
  const publishedAt = movement.data?.publishedAt ?? null;
  const strip = buildMarketStrip(movementState, now, movementRead, publishedAt);
  const freshness = buildFreshness(
    {
      state: movementState,
      read: movementRead,
      symbols,
      quotes: (market.data?.quotes ?? []).map((q) => ({ symbol: q.symbol, ok: q.ok, stale: q.stale, lastCandleAt: q.lastCandleAt })),
      ta: taLoaded ? taRows : undefined,
      publishedAt,
    },
    now,
  );
  const latestTa = latestTaBySymbol(taRows);

  const operational = useQuery({
    queryKey: ["operational-state"],
    queryFn: () => loadOperationalState(),
  });
  const lastRun = operational.data?.runs[0];

  const add = useMutation({
    mutationFn: addSymbol,
    onSuccess: (_data, symbol) => {
      setPending("");
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      toast.success(`${baseAsset(symbol)} added to your watchlist`);
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: ({ id }: { id: string; symbol: string }) => removeSymbol(id),
    onSuccess: (_data, { symbol }) => {
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      toast.success(`${baseAsset(symbol)} removed from your watchlist`);
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const invalidateAfterCheck = () => {
    queryClient.invalidateQueries({ queryKey: ["operational-state"] });
    queryClient.invalidateQueries({ queryKey: ["alerts"] });
    queryClient.invalidateQueries({ queryKey: ["ta"] });
    queryClient.invalidateQueries({ queryKey: movementKey });
  };
  const check = useMutation({
    mutationFn: () => runCheck({ data: undefined }),
    onSuccess: (result) => {
      const { kind, title, description } = describeAlertCheck(result);
      if (kind === "success") {
        toast.success(title, { description });
      } else if (kind === "warning") {
        toast.warning(title, { description });
      } else {
        toast.error(title, { description });
      }
      invalidateAfterCheck();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  // ── Grades: fresh, catching up, stale ─────────────────────────────────────────────────────────
  const movementRefresh = refreshOf(movement);
  const itemOf = (id: FreshnessItem["id"]) => freshness.find((item) => item.id === id)!;
  const gradeItem = (item: FreshnessItem, refresh: RefreshState, canRefresh: boolean, healing: boolean): FreshnessGrade =>
    item.ageMs === null || item.limitMs === null
      ? item.offline ? "stale" : "fresh"
      : gradeFreshness({ ageMs: item.ageMs, limitMs: item.limitMs, crossedAt: item.crossedAt, offline: item.offline,
          now, canRefresh, healing, ...refresh });
  const taFrames = (symbol: string, healing: boolean): TaFrameState[] =>
    TA_ASSESSMENT_FRAMES.map((timeframe, index) => {
      const query = taQueries[index]!;
      const frame = buildTaFrame(
        latestTa.get(`${symbol}|${timeframe}`),
        timeframe,
        now,
        query.isError ? "failed" : query.data === undefined ? "not_loaded" : "loaded",
      );
      const grade: FreshnessGrade =
        frame.candleClosedAt === null
          ? "fresh"
          : gradeFreshness({ ageMs: now - frame.candleClosedAt, limitMs: taLimitMs(timeframe), now,
              canRefresh: taAutomatic.enabled, healing, ...refreshOf(query) });
      return { frame, grade };
    });

  const healCheck = async () => {
    try {
      const result = await runCheck({ data: undefined });
      const { kind, title, description } = describeAlertCheck(result);
      if (kind === "error") toast.error(title, { description });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "The automatic check failed.");
    } finally {
      invalidateAfterCheck();
    }
  };
  // The self-heal watches the underlying problem (past its limit after the display grace), which a poll or a
  // refetch does not interrupt; the grades above only decide what the page shows meanwhile.
  const movementProblem = itemOf("movement").status === "stale" || itemOf("stream").status === "stale";
  const taProblem = symbols.some((symbol) => taFrames(symbol, false).some((t) => t.frame.status === "stale"));
  const heal = useStaleSelfHeal({
    stale: taProblem || movementProblem,
    allowed:
      !settling &&
      runner.canCheckAlerts &&
      settings.data !== undefined &&
      !monitoringPaused &&
      !check.isPending &&
      !strip.machineOffline,
    run: healCheck,
  });
  const catchingUp = settling || heal.healing;
  const movementGrade = worse(
    gradeItem(itemOf("movement"), movementRefresh, automatic.enabled, catchingUp),
    gradeItem(itemOf("stream"), movementRefresh, automatic.enabled, catchingUp),
  );
  const status = stripStatus(strip, movementGrade, settling, movementRefresh.lastAttemptFailed);
  // "Updating…" only looks current for a reading that is itself within its limit (a resume settle or a routine
  // poll); a reading already past its limit stays muted with its "as of" time while it is retried.
  const movementCurrent = status === "live" || (status === "updating" && !movementProblem);
  const framesBySymbol = new Map(symbols.map((symbol) => [symbol, taFrames(symbol, catchingUp)]));
  const staleFrames = [...framesBySymbol.values()].flat().filter((t) => t.grade === "stale");
  const oldestStaleAt = staleFrames.reduce<number | null>((min, t) => {
    const at = t.frame.savedAt ?? t.frame.candleClosedAt;
    return at === null ? min : min === null ? at : Math.min(min, at);
  }, null);
  const taNotice =
    heal.attempted && staleFrames.length && oldestStaleAt !== null
      ? `Technical analysis is catching up (last updated ${formatAge(Math.max(0, now - oldestStaleAt))} ago).`
      : null;

  // Refresh each datum the moment it would cross its limit, not up to an interval later.
  const quoteTimes = (market.data?.quotes ?? []).flatMap((q) => (q.lastCandleAt === null ? [] : [q.lastCandleAt]));
  usePreEmptiveRefresh(
    [
      { key: "market", asOf: quoteTimes.length ? Math.min(...quoteTimes) : null, limitMs: QUOTE_STALE_MS, refetch: () => market.refetch() },
      { key: "movement", asOf: strip.reportedAt, limitMs: PUBLISHED_COPY_OFFLINE_AFTER_MS, refetch: () => movement.refetch() },
      ...TA_ASSESSMENT_FRAMES.map((timeframe, index) => {
        const closes = [...framesBySymbol.values()].flatMap((frames) => {
          const at = frames[index]?.frame.candleClosedAt ?? null;
          return at === null ? [] : [at];
        });
        return {
          key: `ta-${timeframe}`,
          asOf: closes.length ? Math.min(...closes) : null,
          limitMs: taLimitMs(timeframe),
          refetch: () => taQueries[index]?.refetch(),
        };
      }),
    ],
    now,
    automatic.enabled && !settling,
  );

  const available = SUPPORTED_SYMBOLS.filter((s) => !symbols.includes(s));
  const quotes = market.data?.quotes ?? [];
  const sourcesDown = quotes.length > 0 && quotes.every((q) => !q.ok || q.stale);
  const sourceNames = [
    ...new Set(quotes.filter((q) => q.ok && !q.stale && q.source).map((q) => q.source)),
  ]
    .map(sourceLabel)
    .join(", ");
  const runStatus = lastRun ? humanizeReason(lastRun.status) : null;
  const diagnostics: Array<[string, string]> = [
    ["Price source", sourcesDown ? "prices unavailable" : sourceNames || "waiting for the first price load"],
    ...quoteDetailRows(quotes),
    ["Last price refresh", market.data ? new Date(market.data.fetchedAt).toLocaleString() : "not loaded yet"],
    ...(runner.canCheckAlerts
      ? [
          [
            "Last monitoring check",
            lastRun ? `${new Date(lastRun.ran_at).toLocaleString()} (${runStatus!.short})` : "none yet",
          ] as [string, string],
        ]
      : []),
    ["Pairs watched", `${symbols.length} of ${MAX_WATCHLIST_SIZE}`],
  ];
  const checkDisabled = !runner.canCheckAlerts || settings.isPending || settings.isError || monitoringPaused;
  const checkTitle = !runner.canCheckAlerts
    ? undefined
    : monitoringPaused
      ? "Turn on monitoring and market-data collection in the Control room"
      : "Check every watched pair against your alert rule right now";
  const refreshAll = () => {
    void market.refetch();
    void movement.refetch();
    for (const query of taQueries) void query.refetch();
  };

  return (
    <AppShell>
      <PageHeader
        eyebrow="Live market"
        title="Market"
        subtitle={
          automatic.enabled
            ? "Your watched pairs and their latest moves. Updates every minute."
            : "Your watched pairs and their latest moves. Automatic refresh is paused; press Refresh."
        }
        actionsClassName="w-full sm:w-auto"
        actions={
          <>
            <Select value={pending} onValueChange={(v) => add.mutate(v)}>
              <SelectTrigger
                className="h-11 flex-1 sm:h-9 sm:w-[160px] sm:flex-none"
                disabled={add.isPending || available.length === 0}
                aria-label="Add a pair"
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
              className="h-11 flex-1 sm:h-9 sm:flex-none"
              onClick={refreshAll}
              disabled={market.isFetching || symbols.length === 0}
            >
              <RefreshCw
                className={`size-4 ${market.isFetching ? "animate-spin motion-reduce:animate-none" : ""}`}
                aria-hidden
              />
              Refresh
            </Button>
            <Button
              className="h-11 w-full sm:h-9 sm:w-auto"
              onClick={() => {
                if (runner.canCheckAlerts) check.mutate();
              }}
              disabled={checkDisabled || check.isPending}
              title={checkTitle}
            >
              {check.isPending ? (
                <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden />
              ) : (
                <PlayCircle className="size-4" aria-hidden />
              )}
              {check.isPending
                ? "Checking…"
                : monitoringPaused
                  ? "Monitoring paused"
                  : "Check for alerts now"}
            </Button>
          </>
        }
      />

      {symbols.length > 0 ? (
        <MarketStrip
          view={strip}
          status={status}
          current={movementCurrent}
          freshness={freshness}
          now={now}
          taNotice={taNotice}
          diagnostics={diagnostics}
          slow={{ read: slow.data, status: slow.data === undefined && slow.isFetching ? "catching_up" : slowState }}
          checkNow={
            runner.canCheckAlerts
              ? { run: () => check.mutate(), pending: check.isPending, disabled: checkDisabled, title: checkTitle }
              : null
          }
        />
      ) : null}

      <div className="mt-5 grid items-start gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {watchlist.isLoading
          ? [0, 1, 2].map((i) => <QuoteCardSkeleton key={i} symbol="pair" />)
          : symbols.map((symbol) => {
              const quote = quotes.find((q) => q.symbol === symbol);
              return quote ? (
                <QuoteCard
                  key={symbol}
                  quote={quote}
                  now={now}
                  onRetry={() => void market.refetch()}
                  assessment={
                    <PairAssessment
                      now={now}
                      movement={buildPairMovement(symbol, movementState, movementRead, strip.notLiveNote)}
                      movementCurrent={movementCurrent}
                      ta={framesBySymbol.get(symbol) ?? []}
                      healAttempted={heal.attempted}
                      slowRows={slowPairRows(slow.data?.result ?? null, symbol)}
                    />
                  }
                  longerMoves={<LongerMovesLine rows={slowPairRows(slow.data?.result ?? null, symbol)} current={slowShown} />}
                  retrying={automatic.enabled && retrying && down.includes(symbol)}
                  onRemove={() => {
                    const item = watchlist.data?.find((w) => w.symbol === quote.symbol);
                    if (item) remove.mutate({ id: item.id, symbol: item.symbol });
                  }}
                />
              ) : market.isFetching || (!market.data && !market.isError) ? (
                <QuoteCardSkeleton key={symbol} symbol={symbol} />
              ) : (
                <div key={symbol} className="panel p-4 sm:p-5">
                  <h3 className="font-display text-base font-semibold">{baseAsset(symbol)}</h3>
                  <p className="mt-2 text-sm text-muted-foreground">
                    Price unavailable{" "}
                    <button
                      type="button"
                      className="inline-flex min-h-11 items-center text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:min-h-0"
                      onClick={() => void market.refetch()}
                    >
                      Retry
                    </button>
                  </p>
                </div>
              );
            })}
      </div>

      {symbols.length === 0 && !watchlist.isLoading ? (
        <EmptyState
          className="mt-4"
          icon={Plus}
          title="No pairs watched yet"
          body="Add a pair with “Add a pair” above. Its price, recent moves and 24h chart appear here within a minute."
        />
      ) : null}

      {runner.canAnalyse && <MarketRead symbols={symbols} />}
      <TechnicalAnalysis />
    </AppShell>
  );
}

/** One muted "1h −0.4% · 4h −1.9% · 12h −3.2%" line on a pair card; numbers coloured by direction when current. */
function LongerMovesLine({ rows, current }: { rows: ReturnType<typeof slowPairRows>; current: boolean }) {
  const shown = rows.filter((row) => row.returnPct !== null);
  if (!shown.length) return null;
  return (
    <p className="text-xs text-muted-foreground" aria-label="Longer moves">
      {shown.map((row, index) => (
        <span key={row.horizonMin}>
          {index ? " · " : ""}
          {row.label}{" "}
          <span className={`num ${current ? (row.returnPct! > 0 ? "text-bull" : row.returnPct! < 0 ? "text-bear" : "") : ""}`}>
            {`${row.returnPct! > 0 ? "+" : row.returnPct! < 0 ? "−" : ""}${Math.abs(row.returnPct!).toFixed(1)}%`}
          </span>
        </span>
      ))}
    </p>
  );
}
