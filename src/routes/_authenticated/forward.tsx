import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { ArrowDownRight, ArrowUpRight, FlaskConical, Radar, RefreshCw, Satellite, Wallet } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { Hint } from "@/components/hint";
import { EmptyState, PageHeader, SectionTitle, StatusCard, levelStyle } from "@/components/plain";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { FiltersBar, LogsPanel, ResultsPanel } from "@/components/forward-lab";
import { parseSearch, withDefaultRange, type ForwardFilters } from "@/lib/forward/forward-filters";
import { maxDrawdown } from "@/lib/forward/forward-report";
import {
  explainSignalEngine,
  explainTrendTrack,
  type StatusLine,
} from "@/lib/forward/forward-status";
import {
  getForwardDashboard,
  getTrendDashboard,
  runForwardNow,
  runForwardTrendNow,
} from "@/lib/forward.functions";
import { humanizeReason, relativeTime, strategyLabel, summarizeReasons } from "@/lib/labels";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/_authenticated/forward")({
  head: () => ({ meta: [{ title: "Strategy Lab — Crypto Watch" }] }),
  validateSearch: (search: Record<string, unknown>): ForwardFilters => parseSearch(search),
  component: ForwardPage,
});

const time = (ms: number | null | undefined) =>
  ms ? new Date(ms).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
const coin = (symbol: unknown) => String(symbol ?? "").replace(/USDT$/, "");
const frame = (minutes: number) => (minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`);

function runToast(kind: string, summary: { status: string; reason?: string }) {
  const status =
    summary.status === "ok"
      ? "finished"
      : summary.status === "already_done"
        ? "already done for this period"
        : humanizeReason(summary.status).short.toLowerCase();
  const reason = summary.reason ? summarizeReasons(summary.reason).short : "";
  toast(`${kind} ${status}${reason ? `. ${reason}` : ""}`);
}

function Side({ side }: { side: unknown }) {
  const long = side === 1;
  const Icon = long ? ArrowUpRight : ArrowDownRight;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-0.5 rounded-full border px-1.5 text-[11px] font-medium",
        long ? "border-bull/40 text-bull" : "border-bear/40 text-bear",
      )}
    >
      <Icon className="size-3" aria-hidden />
      {long ? "Long" : "Short"}
    </span>
  );
}

function ForwardPage() {
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const filters = useMemo(() => withDefaultRange(search), [search]);
  const setFilters = (patch: Partial<ForwardFilters>) =>
    void navigate({ search: (previous) => ({ ...previous, ...patch }), replace: true });
  const loadTrend = useServerFn(getTrendDashboard);
  // Same query (and key) the trend tab uses; loaded on mount as before so both statuses show.
  const trend = useQuery({ queryKey: ["forward-trend-dashboard"], queryFn: () => loadTrend() });
  const load = useServerFn(getForwardDashboard);
  const data = useQuery({ queryKey: ["forward-dashboard"], queryFn: () => load() });
  const latest = data.data?.latestRun as SignalRun | null | undefined;
  const tracks = trend.data?.tracks ?? [];
  const signalLine = explainSignalEngine(latest, Date.now());
  const trendLine = explainTrendTrack(
    trend.data?.latestAttempt ?? trend.data?.latestRun,
    Math.max(0, ...tracks.map((track) => track.prospectiveDays)),
    tracks.some((track) => track.kind === "variant" && track.currentWeights),
  );

  return (
    <AppShell>
      <div className="space-y-6">
        <PageHeader
          eyebrow="Forward test"
          title="Strategy Lab"
          subtitle="Strategies are tested here with fake money and realistic fees."
        />
        <div
          role="note"
          className="flex items-start gap-2 rounded-lg border border-warn/40 bg-warn/10 p-3 text-sm"
        >
          <FlaskConical className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
          <p>
            <strong className="font-semibold">No strategy has a validated edge; this is a forward test.</strong>{" "}
            <span className="text-muted-foreground">
              Paper results show what happened, not what will happen. Real orders are always placed by you.
            </span>
          </p>
        </div>

        <Tabs defaultValue="signals">
          <TabsList className="h-auto w-full flex-wrap justify-start gap-1 bg-muted/60 p-1 sm:w-auto">
            <LabTab value="signals" line={signalLine} icon={Radar} label="Signals & paper trading" />
            <LabTab value="trend" line={trendLine} icon={Satellite} label="Daily trend portfolios" />
          </TabsList>
          <TabsContent value="signals" className="mt-4">
            <SignalsSection data={data} line={signalLine} filters={filters} onFilters={setFilters} />
          </TabsContent>
          <TabsContent value="trend" className="mt-4">
            <TrendSection data={trend} line={trendLine} />
          </TabsContent>
        </Tabs>
      </div>
    </AppShell>
  );
}

type SignalRun = NonNullable<Parameters<typeof explainSignalEngine>[0]> & {
  wallet_state?: { positions?: Record<string, Record<string, unknown>> } | null;
};

function LabTab({
  value,
  line,
  icon: Icon,
  label,
}: {
  value: string;
  line: StatusLine;
  icon: typeof Radar;
  label: string;
}) {
  return (
    <TabsTrigger value={value} className="min-h-9 gap-2 px-3">
      <Icon className="size-4" aria-hidden />
      {label}
      <span className={cn("size-2 rounded-full", levelStyle(line.level).dot)} aria-hidden />
      <span className="sr-only">({levelStyle(line.level).word})</span>
    </TabsTrigger>
  );
}

function SignalsSection({
  data,
  line,
  filters,
  onFilters,
}: {
  data: UseQueryResult<Awaited<ReturnType<typeof getForwardDashboard>>>;
  line: StatusLine;
  filters: ForwardFilters;
  onFilters: (patch: Partial<ForwardFilters>) => void;
}) {
  const queryClient = useQueryClient();
  const run = useServerFn(runForwardNow);
  const runNow = useMutation({
    mutationFn: () => run(),
    onSuccess: (summary) => {
      runToast("Signal check", summary);
      void queryClient.invalidateQueries({ queryKey: ["forward-dashboard"] });
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Signal check failed"),
  });
  const latest = data.data?.latestRun as SignalRun | null | undefined;
  const equity = data.data?.equity ?? [];
  const positions = Object.entries(latest?.wallet_state?.positions ?? {});
  const signals = (data.data?.signals ?? []).slice(0, 20);
  const openSetups = (data.data?.openSetups ?? []).slice(0, 20);

  return (
    <div className="space-y-6">
      <StatusCard
        level={line.level}
        title={line.title}
        detail={line.detail}
        action={line.action}
        help={line.help}
        raw={line.raw}
      >
        <div className="flex flex-wrap items-center gap-3 pt-2">
          <Button onClick={() => runNow.mutate()} disabled={runNow.isPending}>
            <RefreshCw className={cn("size-4", runNow.isPending && "animate-spin")} aria-hidden />
            Check for new signals now
          </Button>
          {latest ? (
            <span className="text-xs text-muted-foreground">
              Last run for the hour ending {time(latest.boundary_ms)}
            </span>
          ) : null}
        </div>
      </StatusCard>

      <FiltersBar filters={filters} onChange={onFilters} />
      <ResultsPanel filters={filters} />

      <section className="grid gap-6 md:grid-cols-2">
        <div className="panel p-4 sm:p-5">
          <SectionTitle title="Latest signals" hint="Moments a strategy said “enter now” (newest first)." />
          {signals.length === 0 ? (
            <EmptyState
              icon={Radar}
              title="No signals yet"
              body="Signals appear here when a strategy’s condition is met on a completed candle, checked every hour."
              className="py-6"
            />
          ) : (
            <ul className="divide-y divide-border/50 text-sm">
              {signals.map((signal) => (
                <li key={signal.signal_id} className="flex flex-wrap items-center gap-2 py-2">
                  <span className="num w-12 font-medium">{coin(signal.symbol)}</span>
                  <Side side={signal.side} />
                  <span className="min-w-0 flex-1 truncate">{strategyLabel(signal.strategy_id).name}</span>
                  <span className="num text-xs text-muted-foreground" title={time(signal.signal_ms)}>
                    {relativeTime(signal.signal_ms)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="panel p-4 sm:p-5">
          <SectionTitle
            title="Open paper trades"
            hint="Entered with fake money and waiting for target, stop or time limit."
          />
          {openSetups.length === 0 ? (
            <EmptyState
              icon={Satellite}
              title="No open trades"
              body="A trade opens right after a signal and closes at its target, stop or time limit."
              className="py-6"
            />
          ) : (
            <ul className="divide-y divide-border/50 text-sm">
              {openSetups.map((setup) => (
                <li key={setup.setup_id} className="flex flex-wrap items-center gap-2 py-2">
                  <span className="num w-12 font-medium">{coin(setup.symbol)}</span>
                  <Side side={setup.side} />
                  <span className="min-w-0 flex-1 truncate">{strategyLabel(setup.strategy_id).name}</span>
                  <span className="num text-xs text-muted-foreground" title={`Entered ${time(setup.entry_ms)}`}>
                    {relativeTime(setup.entry_ms)} · max {frame(setup.horizon_min)} · {setup.rr}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>

      <section className="grid gap-6 md:grid-cols-[3fr_2fr]">
        <div className="panel p-4 sm:p-5">
          <SectionTitle
            title="Paper wallet"
            hint="Fake-money balance in USDT after closed trades, fees and funding."
          />
          <EquityCurve points={equity} />
        </div>
        <div className="panel p-4 sm:p-5">
          <SectionTitle title="Open positions" hint="What the paper wallet currently holds." />
          {positions.length === 0 ? (
            <EmptyState
              icon={Wallet}
              title="Wallet is flat"
              body="Positions appear while a paper trade is open."
              className="py-6"
            />
          ) : (
            <ul className="divide-y divide-border/50 text-sm">
              {positions.map(([id, position]) => (
                <li key={id} className="flex items-center gap-2 py-2">
                  <span className="num w-12 font-medium">{coin(position["symbol"])}</span>
                  <Side side={position["side"]} />
                  <span className="num ml-auto text-xs text-muted-foreground">
                    {String(position["leverage"])}× leverage
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>

      <LogsPanel filters={filters} />
    </div>
  );
}

const pct = (value: number | null, digits = 3) =>
  value === null ? "—" : `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value * 100).toFixed(digits)}%`;
const day = (ms: number | null | undefined) =>
  ms === null || ms === undefined ? "—" : new Date(ms).toISOString().slice(0, 10);

function TrendSection({
  data,
  line,
}: {
  data: UseQueryResult<Awaited<ReturnType<typeof getTrendDashboard>>>;
  line: StatusLine;
}) {
  const queryClient = useQueryClient();
  const run = useServerFn(runForwardTrendNow);
  const [sample, setSample] = useState<"prospective" | "retrospective">("prospective");
  const runNow = useMutation({
    mutationFn: () => run(),
    onSuccess: (summary) => {
      runToast("Trend portfolio update", summary);
      void queryClient.invalidateQueries({ queryKey: ["forward-trend-dashboard"] });
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "Trend portfolio update failed"),
  });
  const tracks =
    (sample === "prospective" ? data.data?.tracks : data.data?.reconstructedTracks) ?? [];
  const byName = new Map(tracks.map((track) => [track.track, track]));
  const variants = tracks.filter((track) => track.kind === "variant");
  const equalWeight = byName.get("ew_long") ?? null;
  const latest = data.data?.latestRun ?? null;
  return (
    <div className="space-y-6">
      <StatusCard
        level={line.level}
        title={line.title}
        detail={line.detail}
        action={line.action}
        help={line.help}
        raw={line.raw}
      >
        <div className="flex flex-wrap items-center gap-3 pt-2">
          <Button onClick={() => runNow.mutate()} disabled={runNow.isPending}>
            <RefreshCw className={cn("size-4", runNow.isPending && "animate-spin")} aria-hidden />
            Update trend portfolios now
          </Button>
          {latest ? (
            <span className="text-xs text-muted-foreground">
              Scored through {day(latest.through_day_ms)} (daily close, UTC)
            </span>
          ) : null}
        </div>
      </StatusCard>

      <section className="panel p-4 sm:p-5">
        <SectionTitle
          title="Portfolios"
          hint="Each row is a hypothetical daily portfolio across the six coins, compared with simply holding them."
          right={
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              Days
              <select
                aria-label="Trend sample"
                className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground"
                value={sample}
                onChange={(event) => setSample(event.target.value as typeof sample)}
              >
                <option value="prospective">Live days (scored after they happened)</option>
                <option value="retrospective">Back-filled days (replayed from history, less trustworthy)</option>
              </select>
            </label>
          }
        />
        <details className="mb-3 text-xs text-muted-foreground">
          <summary>How these portfolios are simulated</summary>
          <p className="mt-2 max-w-3xl">
            A hypothetical volatility-targeted portfolio per variant, charged 11 bp per unit of turnover plus
            funding, with no margin, liquidation or position limits. No variant has a validated edge. Live
            means the position was recorded before the daily close (it may have been recorded after the open);
            back-filled days are reported separately. Returns are hypothetical open-to-close returns.
          </p>
        </details>
        {data.isPending ? (
          <p className="py-6 text-sm text-muted-foreground">Loading portfolios…</p>
        ) : data.error ? (
          <p role="alert" className="py-6 text-sm text-destructive">
            Portfolios could not be loaded: {data.error.message}
          </p>
        ) : variants.length === 0 ? (
          <EmptyState
            icon={Satellite}
            title="No portfolios yet"
            body="They appear after the first successful update. Scored days follow each daily close (00:00 UTC)."
          />
        ) : (
          <div className="-mx-1 overflow-x-auto px-1" tabIndex={0} aria-label="Trend portfolios">
            <table className="w-full min-w-[900px] text-left text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="py-2 pr-3 font-medium">Portfolio</th>
                  <th className="py-2 pr-3 text-right font-medium">
                    <Hint text="Live days (scored after they happened) / back-filled days (replayed from history, less trustworthy).">
                      Live / back-filled days
                    </Hint>
                  </th>
                  <th className="py-2 pr-3 text-right font-medium">Avg per day</th>
                  <th className="py-2 pr-3 text-right font-medium">
                    <Hint term="volTarget">Hold all, same vol</Hint>
                  </th>
                  <th className="py-2 pr-3 text-right font-medium">
                    <Hint text="Always long every coin with equal weight. The simplest baseline to beat.">
                      Hold all equally
                    </Hint>
                  </th>
                  <th className="py-2 pr-3 text-right font-medium">
                    <Hint term="turnover">Traded / day</Hint>
                  </th>
                  <th className="py-2 pr-3 text-right font-medium">
                    <Hint term="gross">Exposure</Hint>
                  </th>
                  <th className="py-2 pr-3 font-medium">Current positions</th>
                  <th className="py-2 font-medium">
                    <Hint term="mde">Smallest edge we could detect</Hint>
                  </th>
                </tr>
              </thead>
              <tbody>
                {variants.map((track) => {
                  const control = track.control ? byName.get(track.control) : undefined;
                  const label = strategyLabel(track.track);
                  return (
                    <tr key={track.track} className="border-b border-border/50 align-top">
                      <td className="max-w-56 py-2 pr-3">
                        <Hint text={`${label.blurb} (id ${track.track})`}>
                          <span className="font-medium">{label.name}</span>
                        </Hint>
                      </td>
                      <td className="num py-2 pr-3 text-right">
                        {track.prospectiveDays} / {track.retrospectiveDays}
                      </td>
                      <td
                        className={cn(
                          "num py-2 pr-3 text-right",
                          track.meanDaily === null ? "" : track.meanDaily > 0 ? "text-bull" : "text-bear",
                        )}
                      >
                        {pct(track.meanDaily)}
                      </td>
                      <td className="num py-2 pr-3 text-right text-muted-foreground">
                        {pct(control?.meanDaily ?? null)}
                      </td>
                      <td className="num py-2 pr-3 text-right text-muted-foreground">
                        {pct(equalWeight?.meanDaily ?? null)}
                      </td>
                      <td className="num py-2 pr-3 text-right">
                        {track.meanTurnover === null ? "—" : `${(track.meanTurnover * 100).toFixed(1)}%`}
                      </td>
                      <td className="num py-2 pr-3 text-right">
                        {track.meanGross === null ? "—" : `${track.meanGross.toFixed(2)}×`}
                      </td>
                      <td className="py-2 pr-3">
                        {track.currentWeights ? (
                          <Weights
                            weights={track.currentWeights.weights}
                            note={`For ${day(track.currentWeights.day_ms)} (UTC). Decided ${time(track.currentWeights.decided_at_ms)}; recorded ${time(track.currentWeights.recorded_at_ms)} (${track.currentWeights.sample_kind === "prospective" ? "live" : "back-filled"}).`}
                          />
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="num py-2 text-xs">
                        <Hint text={track.edgeLine}>
                          {track.muMinDaily === null
                            ? `${track.days} days: too few`
                            : `${pct(track.muMinDaily)} / day`}
                        </Hint>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {variants.length ? (
        <section className="panel p-4 sm:p-5">
          <SectionTitle
            title="Growth of 1 unit"
            hint={`Hypothetical equity, ${sample === "prospective" ? "live days" : "back-filled days"}.`}
            right={<CurveLegend />}
          />
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {variants.map((track) => (
              <div key={track.track} className="rounded-md border border-border/60 p-3">
                <h3 className="truncate text-sm font-medium">{strategyLabel(track.track).name}</h3>
                <MultiCurve
                  series={[
                    track.equity,
                    (track.control ? byName.get(track.control)?.equity : undefined) ?? [],
                    equalWeight?.equity ?? [],
                  ]}
                />
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function Weights({ weights, note }: { weights: Record<string, number>; note: string }) {
  const entries = Object.entries(weights);
  const max = Math.max(1, ...entries.map(([, w]) => Math.abs(w)));
  return (
    <div className="min-w-44" title={note}>
      <ul className="space-y-0.5">
        {entries.map(([symbol, weight]) => {
          const share = (Math.abs(weight) / max) * 50;
          return (
            <li key={symbol} className="grid grid-cols-[2.75rem_1fr_3rem] items-center gap-1.5 text-[11px]">
              <span className="num">{coin(symbol)}</span>
              <span className="relative h-1.5 rounded-full bg-muted" aria-hidden>
                <span className="absolute inset-y-0 left-1/2 w-px bg-muted-foreground/50" />
                <span
                  className={cn("absolute inset-y-0 rounded-full", weight >= 0 ? "bg-bull" : "bg-bear")}
                  style={weight >= 0 ? { left: "50%", width: `${share}%` } : { right: "50%", width: `${share}%` }}
                />
              </span>
              <span className={cn("num text-right", weight > 0 ? "text-bull" : weight < 0 ? "text-bear" : "text-muted-foreground")}>
                {weight > 0 ? "+" : ""}
                {weight.toFixed(2)}
              </span>
            </li>
          );
        })}
      </ul>
      <p className="mt-1 text-[10px] text-muted-foreground">+ long · − short · ×equity</p>
    </div>
  );
}

const CURVES = [
  { className: "text-primary", dash: undefined, label: "Strategy" },
  { className: "text-aurora", dash: "4 3", label: "Hold all, same vol" },
  { className: "text-warn", dash: "1 3", label: "Hold all equally" },
] as const;

function CurveLegend() {
  return (
    <ul className="flex flex-wrap gap-3 text-xs text-muted-foreground">
      {CURVES.map((curve) => (
        <li key={curve.label} className="flex items-center gap-1.5">
          <svg width="20" height="6" className={curve.className} aria-hidden>
            <line x1="0" y1="3" x2="20" y2="3" stroke="currentColor" strokeWidth="2" strokeDasharray={curve.dash} />
          </svg>
          {curve.label}
        </li>
      ))}
    </ul>
  );
}

function MultiCurve({ series }: { series: { day_ms: number; equity: number }[][] }) {
  const points = series.flat();
  if (series[0]!.length < 2)
    return <p className="py-6 text-sm text-muted-foreground">Not enough days yet.</p>;
  const width = 300;
  const height = 100;
  const xs = points.map((p) => p.day_ms);
  const ys = points.map((p) => p.equity);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const path = (line: { day_ms: number; equity: number }[]) =>
    line
      .map(
        (p, i) =>
          `${i ? "L" : "M"}${((p.day_ms - x0) / (x1 - x0 || 1)) * width},${height - ((p.equity - y0) / (y1 - y0 || 1)) * height}`,
      )
      .join(" ");
  const last = series[0]!.at(-1)!.equity;
  return (
    <>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="mt-2 h-24 w-full overflow-visible"
        role="img"
        aria-label={`Hypothetical equity, strategy now at ${last.toFixed(3)}`}
        preserveAspectRatio="none"
      >
        {series.map((line, index) =>
          line.length > 1 ? (
            <path
              key={index}
              d={path(line)}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeDasharray={CURVES[index]?.dash}
              vectorEffect="non-scaling-stroke"
              className={CURVES[index]?.className}
            />
          ) : null,
        )}
      </svg>
      <p className="num mt-1 text-[11px] text-muted-foreground">
        Now {last.toFixed(3)} · range {y0.toFixed(3)}–{y1.toFixed(3)}
      </p>
    </>
  );
}

function EquityCurve({ points }: { points: { ms: number; balance: number }[] }) {
  if (points.length < 2)
    return (
      <EmptyState
        icon={Wallet}
        title="No wallet history yet"
        body="The balance line starts after the first paper trade closes."
        className="py-6"
      />
    );
  const width = 480;
  const height = 160;
  const xs = points.map((p) => p.ms);
  const ys = points.map((p) => p.balance);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const coords = points.map(
    (p) =>
      [((p.ms - x0) / (x1 - x0 || 1)) * width, height - ((p.balance - y0) / (y1 - y0 || 1)) * height] as const,
  );
  const path = coords.map(([x, y], i) => `${i ? "L" : "M"}${x},${y}`).join(" ");
  const first = points[0]!.balance;
  const last = points.at(-1)!.balance;
  const change = first ? (last - first) / first : 0;
  const drawdown = maxDrawdown(points);
  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-x-3">
        <span className="num text-2xl font-semibold">
          {last.toLocaleString(undefined, { maximumFractionDigits: 2 })} USDT
        </span>
        <span className={cn("num text-sm", change > 0 ? "text-bull" : change < 0 ? "text-bear" : "text-muted-foreground")}>
          {change > 0 ? "▲ +" : change < 0 ? "▼ −" : ""}
          {Math.abs(change * 100).toFixed(2)}% since start
        </span>
        {drawdown ? (
          <span className="num text-sm text-muted-foreground">
            <Hint text="Largest fall from a peak to a later low of the wallet balance, measured on the hourly (daily after 60 days) points.">
              Max drawdown
            </Hint>{" "}
            −{drawdown.usdt.toLocaleString(undefined, { maximumFractionDigits: 2 })} USDT (−
            {(drawdown.fraction * 100).toFixed(2)}%)
          </span>
        ) : null}
      </div>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="mt-3 h-40 w-full text-primary"
        role="img"
        aria-label={`Paper wallet balance, now ${last.toFixed(2)} USDT`}
        preserveAspectRatio="none"
      >
        <defs>
          <linearGradient id="wallet-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.3" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={`${path} L${width},${height} L0,${height} Z`} fill="url(#wallet-fill)" />
        <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      </svg>
      <p className="num mt-1 text-[11px] text-muted-foreground">
        {time(points[0]!.ms)} → {time(points.at(-1)!.ms)} · realised balance only
      </p>
    </div>
  );
}
