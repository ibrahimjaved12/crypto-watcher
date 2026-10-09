import {
  PageHeader,
  TechnicalDetails,
  EmptyState,
  QueryNotice,
  SignedBar,
  StatusCard,
} from "@/components/presentation";
import { Hint } from "@/components/hint";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  monitoringRunLabel,
  strategyLabel,
  strategyBlurb,
  trendVariantLabel,
  benchmarkLabel,
  sampleLabel,
  pairLabel,
  issueSummary,
} from "@/lib/presentation/labels";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { summarizeOutcomes } from "@/lib/forward/forward-dashboard";
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

export const Route = createFileRoute("/_authenticated/forward")({
  head: () => ({ meta: [{ title: "Strategy Lab — Crypto Watch" }] }),
  component: ForwardPage,
});

const time = (ms: number | null | undefined) =>
  ms
    ? new Date(ms).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
const r = (value: number | null) =>
  value === null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)} R`;

function Side({ side }: { side: number }) {
  return side === 1 ? (
    <span className="chip" data-tone="bull">
      <ArrowUpRight className="size-3" aria-hidden /> Long
    </span>
  ) : (
    <span className="chip" data-tone="bear">
      <ArrowDownRight className="size-3" aria-hidden /> Short
    </span>
  );
}

function ForwardPage() {
  const queryClient = useQueryClient();
  const load = useServerFn(getForwardDashboard);
  const run = useServerFn(runForwardNow);
  const [sinceDays, setSinceDays] = useState(30);
  const data = useQuery({ queryKey: ["forward-dashboard"], queryFn: () => load() });
  const runNow = useMutation({
    mutationFn: () => run(),
    onSuccess: (summary) => {
      toast(
        `Signal evaluation: ${monitoringRunLabel(summary.status)}${summary.reason ? ` · ${issueSummary(summary.reason)}` : ""}`,
      );
      void queryClient.invalidateQueries({ queryKey: ["forward-dashboard"] });
    },
    onError: (error) =>
      toast.error(
        error instanceof Error ? issueSummary(error.message) : "Signal evaluation failed",
      ),
  });
  const summary = useMemo(
    () => summarizeOutcomes(data.data?.outcomeRows ?? [], Date.now() - sinceDays * 86_400_000),
    [data.data, sinceDays],
  );
  const latest = data.data?.latestRun as
    | {
        status: string;
        reason: string | null;
        boundary_ms: number;
        wallet_state: { positions?: Record<string, Record<string, unknown>> } | null;
      }
    | null
    | undefined;
  const fresh = latest?.status === "ok" && Date.now() - latest.boundary_ms < 2 * 3_600_000;
  const equity = data.data?.equity ?? [];
  const positions = Object.entries(latest?.wallet_state?.positions ?? {});

  return (
    <AppShell>
      <div className="space-y-6">
        <PageHeader
          title="Strategy Lab"
          description="Strategies are tested here with fake money and realistic fees, on market data that arrives after each decision."
        />
        <div className="rounded-xl border border-warn/40 bg-warn/10 p-4 text-sm text-warn">
          This is a forward test. No strategy has a validated edge yet, and paper results are not a
          prediction of profit.
        </div>
        <StatusSummary signalRun={latest} signalPending={data.isPending} signalError={data.error} />
        <Tabs defaultValue="signals">
          <TabsList className="mb-5 h-auto flex-wrap">
            <TabsTrigger value="signals">Signals &amp; Paper Trading</TabsTrigger>
            <TabsTrigger value="portfolios">Daily Trend Portfolios</TabsTrigger>
          </TabsList>
          <TabsContent value="signals" className="space-y-6">
            <div className="flex flex-wrap items-center gap-3">
              <div>
                <h2 className="text-xl font-semibold">Signals &amp; Paper Trading</h2>
                <p className="mt-1 text-xs text-muted-foreground">
                  Every hour, each strategy may fire a signal; a fake-money wallet takes the trade
                  with fees, funding and a stop.
                </p>
              </div>
              <Badge variant={fresh ? "secondary" : "outline"}>
                {latest
                  ? `${fresh ? "Up to date" : "Not recent"} · last check ${time(latest.boundary_ms)}`
                  : "No checks yet"}
              </Badge>
              <Button
                className="sm:ml-auto"
                size="sm"
                onClick={() => runNow.mutate()}
                disabled={runNow.isPending}
              >
                {runNow.isPending ? "Checking…" : "Check for new signals now"}
              </Button>
            </div>
            <QueryNotice pending={data.isPending} error={data.error} />
            {runNow.data && (
              <p role="status" className="text-sm">
                {monitoringRunLabel(runNow.data.status)}
                {runNow.data.reason ? ` · ${issueSummary(runNow.data.reason)}` : ""}
              </p>
            )}
            {(latest || runNow.error || runNow.data) && (
              <TechnicalDetails title="Technical details (raw run record)">
                <pre className="whitespace-pre-wrap">
                  {JSON.stringify(
                    {
                      latestRun: latest,
                      evaluation: runNow.data,
                      error: runNow.error instanceof Error ? runNow.error.message : null,
                    },
                    null,
                    2,
                  )}
                </pre>
              </TechnicalDetails>
            )}

            <section className="panel p-5">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <h2 className="font-medium">Results by strategy</h2>
                <select
                  aria-label="Outcome lookback period"
                  className="rounded border bg-background px-2 py-1 text-sm"
                  value={sinceDays}
                  onChange={(event) => setSinceDays(Number(event.target.value))}
                >
                  {[7, 30, 90, 365].map((days) => (
                    <option key={days} value={days}>
                      last {days} days
                    </option>
                  ))}
                </select>
              </div>
              <p className="mb-4 text-xs text-muted-foreground">
                Finished paper trades only. Each strategy is shown next to a random baseline that
                trades equally often; beating it is the minimum bar, not proof of an edge.
              </p>
              {!data.isPending && !data.error && summary.length === 0 ? (
                <EmptyState title="No finished trades yet">
                  The first results appear after a signal fires and its time limit passes; this can
                  take hours. Try a longer period to include earlier results.
                </EmptyState>
              ) : (
                <div
                  className="overflow-x-auto"
                  tabIndex={0}
                  role="region"
                  aria-label="Strategy outcomes"
                >
                  <table className="data-table">
                    <thead>
                      <tr className="text-left text-muted-foreground">
                        <th>Strategy</th>
                        <th>Trades</th>
                        <th>Wins</th>
                        <th>
                          <Hint text="Target and stop were both touched inside one candle, so the order is unknown. Counted with the worse result.">
                            Unclear results
                          </Hint>
                        </th>
                        <th>
                          <Hint term="R">Average result / trade</Hint>
                        </th>
                        <th>
                          <Hint term="placebo">Random baseline</Hint>
                        </th>
                        <th>
                          <Hint term="rr">Reward : risk</Hint>
                        </th>
                        <th>
                          <Hint term="scoreBand">Score band</Hint>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {summary.map((row) => (
                        <tr key={`${row.strategyId}|${row.version}|${row.rr}`} className="border-t">
                          <td className="min-w-56">
                            <span className="font-medium">{strategyLabel(row.strategyId)}</span>
                            <p className="mt-0.5 text-xs text-muted-foreground">
                              {strategyBlurb(row.strategyId)}
                            </p>
                            <TechnicalDetails>
                              <p>
                                {row.strategyId} · {row.version}
                              </p>
                            </TechnicalDetails>
                          </td>
                          <td className="num">{row.n}</td>
                          <td className="num">{row.wins}</td>
                          <td className="num">{row.ambiguous}</td>
                          <td
                            className={`num ${(row.meanNetR ?? 0) > 0 ? "text-bull" : (row.meanNetR ?? 0) < 0 ? "text-bear" : ""}`}
                          >
                            {r(row.meanNetR)}
                          </td>
                          <td className="num text-muted-foreground">
                            {row.placebo
                              ? `${r(row.placebo.meanNetR)} over ${row.placebo.n} trades`
                              : "—"}
                          </td>
                          <td className="num">{row.rr}</td>
                          <td className="text-xs text-muted-foreground">not scored yet</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="grid gap-4 md:grid-cols-2">
              <div className="panel p-5">
                <h2 className="mb-3 font-semibold">Latest signals</h2>
                <ul className="space-y-1 text-sm">
                  {!data.isPending && !data.error && !data.data?.signals.length && (
                    <li className="text-muted-foreground">
                      No signals yet. They appear when a strategy's entry rule fires.
                    </li>
                  )}
                  {(data.data?.signals ?? []).slice(0, 20).map((signal) => (
                    <li
                      className="border-b border-border/60 py-3 last:border-0"
                      key={signal.signal_id}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <Side side={signal.side} />
                        <span className="font-medium">{pairLabel(signal.symbol)}</span>
                        <span className="text-muted-foreground">{time(signal.signal_ms)}</span>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {strategyLabel(signal.strategy_id)}
                      </p>
                      <TechnicalDetails>
                        {signal.strategy_id} · {signal.version}
                      </TechnicalDetails>
                    </li>
                  ))}
                </ul>
              </div>
              <div className="panel p-5">
                <h2 className="mb-3 font-semibold">Open trades (waiting for a result)</h2>
                <ul className="space-y-1 text-sm">
                  {!data.isPending && !data.error && !data.data?.openSetups.length && (
                    <li className="text-muted-foreground">
                      No open trades. A trade opens when a signal fires and closes at its target,
                      stop or time limit.
                    </li>
                  )}
                  {(data.data?.openSetups ?? []).slice(0, 20).map((setup) => (
                    <li
                      className="border-b border-border/60 py-3 last:border-0"
                      key={setup.setup_id}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <Side side={setup.side} />
                        <span className="font-medium">{pairLabel(setup.symbol)}</span>
                        <span className="text-muted-foreground">entered {time(setup.entry_ms)}</span>
                        <span className="num text-xs text-muted-foreground">reward:risk {setup.rr}</span>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {strategyLabel(setup.strategy_id)}
                      </p>
                      <TechnicalDetails>
                        {setup.strategy_id} · {setup.version}
                      </TechnicalDetails>
                    </li>
                  ))}
                </ul>
              </div>
            </section>

            <section className="grid gap-4 md:grid-cols-2">
              <div className="panel p-5">
                <h2 className="mb-1 font-semibold">Paper wallet balance</h2>
                <p className="mb-3 text-xs text-muted-foreground">
                  Fake money (USDT), realised trades only, after fees and funding.
                </p>
                {!data.isPending && !data.error && <EquityCurve points={equity} />}
              </div>
              <div className="panel p-5">
                <h2 className="mb-3 font-semibold">Open paper positions</h2>
                <ul className="space-y-1 text-sm">
                  {positions.map(([id, position]) => (
                    <li key={id} className="flex flex-wrap items-center gap-2">
                      <Side side={Number(position["side"])} />
                      <span className="font-medium">{pairLabel(String(position["symbol"]))}</span>
                      <span className="num text-muted-foreground">
                        {String(position["leverage"])}× leverage
                      </span>
                    </li>
                  ))}
                  {!data.isPending && !data.error && positions.length === 0 ? (
                    <li className="text-muted-foreground">No open paper positions.</li>
                  ) : null}
                </ul>
              </div>
            </section>
          </TabsContent>
          <TabsContent value="portfolios">
            <TrendSection />
          </TabsContent>
        </Tabs>
      </div>
    </AppShell>
  );
}

const pct = (value: number | null, digits = 3) =>
  value === null ? "—" : `${(value * 100).toFixed(digits)}%`;
const signedPct = (value: number | null) =>
  value === null ? "—" : `${value > 0 ? "+" : ""}${(value * 100).toFixed(3)}%`;
const day = (ms: number | null | undefined) =>
  ms === null || ms === undefined ? "—" : new Date(ms).toISOString().slice(0, 10);


function StatusSummary({
  signalRun,
  signalPending,
  signalError,
}: {
  signalRun: Parameters<typeof explainSignalEngine>[0];
  signalPending: boolean;
  signalError: unknown;
}) {
  const load = useServerFn(getTrendDashboard);
  const trend = useQuery({ queryKey: ["forward-trend-dashboard"], queryFn: () => load() });
  const tracks = trend.data?.tracks ?? [];
  const lines: StatusLine[] = [
    signalError
      ? {
          level: "problem",
          title: "Signals & paper trading",
          detail: "Status could not be loaded. Refresh to try again.",
        }
      : signalPending
        ? { level: "wait", title: "Signals & paper trading", detail: "Loading status…" }
        : explainSignalEngine(signalRun, Date.now()),
    trend.error
      ? {
          level: "problem",
          title: "Daily trend portfolios",
          detail: "Status could not be loaded. Refresh to try again.",
        }
      : trend.isPending
        ? { level: "wait", title: "Daily trend portfolios", detail: "Loading status…" }
        : explainTrendTrack(
            trend.data?.latestAttempt ?? trend.data?.latestRun,
            Math.max(0, ...tracks.map((track) => track.prospectiveDays)),
            tracks.some((track) => track.kind === "variant" && track.currentWeights),
          ),
  ];
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {lines.map((line) => (
        <StatusCard
          key={line.title}
          level={line.level}
          title={line.title}
          detail={line.detail}
          action={line.action}
          raw={line.raw}
        >
          {line.level !== "ok" && (
            <p>
              Signals need up to 260 completed candles per timeframe from the market-data
              collector. When running locally, start everything with{" "}
              <code>npm run dev:local:all</code>.
            </p>
          )}
          {trend.error && line.title.startsWith("Daily") && <p>{trend.error.message}</p>}
        </StatusCard>
      ))}
    </div>
  );
}

function TrendSection() {
  const queryClient = useQueryClient();
  const load = useServerFn(getTrendDashboard);
  const run = useServerFn(runForwardTrendNow);
  const [selectedVariant, setSelectedVariant] = useState("");
  const [showChartValues, setShowChartValues] = useState(false);
  const [sample, setSample] = useState<"prospective" | "retrospective">("prospective");
  const data = useQuery({ queryKey: ["forward-trend-dashboard"], queryFn: () => load() });
  const runNow = useMutation({
    mutationFn: () => run(),
    onSuccess: (summary) => {
      toast(
        `Portfolio evaluation: ${monitoringRunLabel(summary.status)}${summary.reason ? ` · ${issueSummary(summary.reason)}` : ""}`,
      );
      void queryClient.invalidateQueries({ queryKey: ["forward-trend-dashboard"] });
    },
    onError: (error) =>
      toast.error(
        error instanceof Error ? issueSummary(error.message) : "Portfolio evaluation failed",
      ),
  });
  const tracks =
    (sample === "prospective" ? data.data?.tracks : data.data?.reconstructedTracks) ?? [];
  const byName = new Map(tracks.map((track) => [track.track, track]));
  const variants = tracks.filter((track) => track.kind === "variant");
  const equalWeight = byName.get("ew_long") ?? null;
  const latest = data.data?.latestRun ?? null;
  const chartVariants = variants.filter((track) => track.equity.length >= 2);
  const selected =
    chartVariants.find((track) => track.track === selectedVariant) ?? chartVariants[0];
  return (
    <section className="space-y-3">
      <p className="text-sm text-muted-foreground">
        Hypothetical daily portfolios across six coins: the position is decided from yesterday's
        close and held for the day, with fees (11 bp per unit traded) and funding, but no margin or
        liquidation model. No variant has a validated edge. Back-filled days are not forward
        evidence.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-xl font-semibold">Daily Trend Portfolios</h2>
        <Badge variant={!latest ? "outline" : latest.status === "ok" ? "secondary" : "destructive"}>
          {latest
            ? `${monitoringRunLabel(latest.status)} · scored through ${day(latest.through_day_ms)} (UTC)`
            : data.isPending
              ? "Loading…"
              : data.error
                ? "Unavailable"
                : "No evaluations yet"}
        </Badge>
        {latest?.reason ? (
          <span className="text-xs text-warn">{issueSummary(latest.reason)}</span>
        ) : null}
        <Button size="sm" onClick={() => runNow.mutate()} disabled={runNow.isPending}>
          {runNow.isPending ? "Updating…" : "Update trend portfolios now"}
        </Button>
        <select
          aria-label="Trend sample"
          className="rounded border bg-background px-2 py-1 text-sm"
          value={sample}
          onChange={(event) => setSample(event.target.value as typeof sample)}
        >
          <option value="prospective">Live days (scored after they happened)</option>
          <option value="retrospective">Back-filled days (replayed, less trustworthy)</option>
        </select>
      </div>
      {data.data?.latestAttempt?.reason ? (
        <p className="text-xs text-muted-foreground">
          Latest attempt: {issueSummary(data.data.latestAttempt.reason)}
        </p>
      ) : null}
      <QueryNotice pending={data.isPending} error={data.error} />
      {runNow.data && (
        <p role="status" className="text-sm">
          {monitoringRunLabel(runNow.data.status)}
          {runNow.data.reason ? ` · ${issueSummary(runNow.data.reason)}` : ""}
        </p>
      )}
      <TechnicalDetails title="Technical details (raw run record)">
        <pre className="whitespace-pre-wrap">
          {JSON.stringify(
            {
              lastRun: latest && {
                status: latest.status,
                reason: latest.reason,
                run_key: latest.run_key,
                created_at: latest.created_at,
              },
              latestAttempt: data.data?.latestAttempt,
              evaluation: runNow.data,
              error: runNow.error instanceof Error ? runNow.error.message : null,
            },
            null,
            2,
          )}
        </pre>
      </TechnicalDetails>
      <p className="text-xs text-muted-foreground">
        Statistics use the selected sample. Position bars show each coin's weight as a multiple of
        the portfolio: right/green = long, left/red = short, empty = flat (up to ±2×).
      </p>
      <div
        className="overflow-x-auto"
        tabIndex={0}
        role="region"
        aria-label="Daily portfolio results"
      >
        <table className="data-table">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th>Portfolio</th>
              <th>
                <Hint term="live">Live</Hint> / <Hint term="backfilled">back-filled</Hint> days
              </th>
              <th>Average day</th>
              <th>
                <Hint term="volTarget">Buy &amp; hold</Hint>
              </th>
              <th>Equal-weight</th>
              <th>
                <Hint term="turnover">Traded / day</Hint>
              </th>
              <th>
                <Hint term="gross">Exposure</Hint>
              </th>
              <th>Current positions</th>
              <th>
                <Hint term="mde">Enough data?</Hint>
              </th>
            </tr>
          </thead>
          <tbody>
            {variants.map((track) => {
              const control = track.control ? byName.get(track.control) : undefined;
              return (
                <tr key={track.track} className="border-t align-top">
                  <td className="min-w-64">
                    <span className="font-medium">{trendVariantLabel(track.track)}</span>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      {strategyBlurb(track.track)}
                    </p>
                    <TechnicalDetails>
                      <p>Variant: {track.track}</p>
                      <p>Control: {track.control ?? "none"}</p>
                      <p>Sample: {track.sampleKind}</p>
                    </TechnicalDetails>
                  </td>
                  <td className="num">
                    {track.prospectiveDays} / {track.retrospectiveDays}
                  </td>
                  <td
                    className={`num ${(track.meanDaily ?? 0) > 0 ? "text-bull" : (track.meanDaily ?? 0) < 0 ? "text-bear" : ""}`}
                  >
                    {signedPct(track.meanDaily)}
                  </td>
                  <td>
                    <span className="num" title={track.control ? benchmarkLabel(track.control) : undefined}>
                      {signedPct(control?.meanDaily ?? null)}
                    </span>
                  </td>
                  <td className="num">{signedPct(equalWeight?.meanDaily ?? null)}</td>
                  <td className="num">
                    {track.meanTurnover === null ? "—" : `${(track.meanTurnover * 100).toFixed(1)}%`}
                  </td>
                  <td className="num">
                    {track.meanGross === null ? "—" : `${track.meanGross.toFixed(2)}×`}
                  </td>
                  <td className="text-xs">
                    {track.currentWeights ? (
                      <>
                        <p className="mb-2 text-muted-foreground">
                          for {day(track.currentWeights.day_ms)} (UTC day)
                        </p>
                        <ul className="min-w-56 space-y-1">
                          {Object.entries(track.currentWeights.weights).map(([symbol, weight]) => (
                            <li key={symbol} className="grid grid-cols-[3rem_auto] items-center gap-2">
                              <span>{symbol.replace(/USDT$/, "")}</span>
                              <SignedBar
                                value={weight}
                                max={2}
                                label={
                                  weight === 0
                                    ? "flat"
                                    : `${weight > 0 ? "+" : ""}${weight.toFixed(2)}× ${weight > 0 ? "long" : "short"}`
                                }
                              />
                            </li>
                          ))}
                        </ul>
                        <TechnicalDetails>
                          Decided {time(track.currentWeights.decided_at_ms)}; recorded{" "}
                          {time(track.currentWeights.recorded_at_ms)} (
                          {sampleLabel(track.currentWeights.sample_kind)})
                        </TechnicalDetails>
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="min-w-52 text-xs">
                    <p>
                      {track.muMinDaily === null
                        ? sample === "prospective"
                          ? "Not yet: too few live days"
                          : "Not yet: too few back-filled days"
                        : `${track.days} days: only an average above ${pct(track.muMinDaily)}/day could be told apart from noise`}
                    </p>
                    <details className="mt-2">
                      <summary>What this means</summary>
                      <p className="mt-2 text-muted-foreground">
                        {track.edgeLine}. Minimum detectable edge is a power estimate (how big an
                        effect this many days could reliably detect), not a significance test.
                      </p>
                    </details>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {!data.isPending && !data.error && !variants.length && (
        <EmptyState title="No daily portfolio results yet">
          Positions are recorded once full daily price history is available. The first scored day
          appears after the next daily close (00:00 UTC).
        </EmptyState>
      )}
      {!data.isPending &&
        !data.error &&
        (selected ? (
          <div className="panel space-y-4 p-5">
            <div className="flex flex-wrap items-center gap-3">
              <h3 className="font-semibold">Growth of 1 unit (portfolio vs benchmarks)</h3>
              <select
                aria-label="Portfolio variant to chart"
                className="max-w-full rounded-md border bg-background p-2 text-sm"
                value={selected.track}
                onChange={(event) => setSelectedVariant(event.target.value)}
              >
                {chartVariants.map((track) => (
                  <option key={track.track} value={track.track}>
                    {trendVariantLabel(track.track)}
                  </option>
                ))}
              </select>
            </div>
            <p className="text-xs text-muted-foreground">
              {sampleLabel(sample)} · normalized equity · daily dates in UTC
            </p>
            <MultiCurve
              series={[
                selected.equity,
                (selected.control ? byName.get(selected.control)?.equity : undefined) ?? [],
                equalWeight?.equity ?? [],
              ]}
            />
            <ul className="flex flex-wrap gap-4 text-xs">
              <li className="text-primary">Solid — {trendVariantLabel(selected.track)}</li>
              <li className="text-muted-foreground">
                Dashed —{" "}
                {selected.control ? benchmarkLabel(selected.control) : "Matched buy & hold"}
                {(selected.control ? (byName.get(selected.control)?.equity.length ?? 0) : 0) < 2
                  ? " (not enough observations)"
                  : ""}
              </li>
              <li className="text-warn">
                Dotted — {benchmarkLabel("ew_long")}
                {(equalWeight?.equity.length ?? 0) < 2 ? " (not enough observations)" : ""}
              </li>
            </ul>
            <details
              className="text-xs"
              onToggle={(event) => setShowChartValues(event.currentTarget.open)}
            >
              <summary>Latest 30 chart values</summary>
              <div className="mt-2 max-h-64 overflow-auto">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Date (UTC)</th>
                      <th>Variant equity</th>
                      <th>Buy &amp; hold equity</th>
                      <th>Equal-weight equity</th>
                    </tr>
                  </thead>
                  <tbody>
                    {showChartValues &&
                      selected.equity.slice(-30).map((point) => (
                        <tr key={point.day_ms}>
                          <td>{day(point.day_ms)}</td>
                          <td>{point.equity.toFixed(4)}</td>
                          <td>
                            {(selected.control
                              ? byName
                                  .get(selected.control)
                                  ?.equity.find((p) => p.day_ms === point.day_ms)
                                  ?.equity.toFixed(4)
                              : undefined) ?? "—"}
                          </td>
                          <td>
                            {equalWeight?.equity
                              .find((p) => p.day_ms === point.day_ms)
                              ?.equity.toFixed(4) ?? "—"}
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </details>
          </div>
        ) : variants.length > 0 ? (
          <EmptyState title="The chart needs at least two scored days">
            Curves appear after two days are scored in this sample. You can switch to back-filled
            days to see the replayed history.
          </EmptyState>
        ) : null)}
    </section>
  );
}

function MultiCurve({ series }: { series: { day_ms: number; equity: number }[][] }) {
  const points = series.flat();
  if (series[0]!.length < 2)
    return <p className="text-sm text-muted-foreground">Not enough days yet.</p>;
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
  const styles = ["text-primary", "text-muted-foreground", "text-amber-500"];
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="h-52 w-full"
      role="img"
      aria-label="Normalized portfolio equity: solid variant, dashed buy and hold, dotted equal-weight benchmark"
    >
      {series.map((line, index) =>
        line.length > 1 ? (
          <path
            key={index}
            d={path(line)}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.2"
            strokeDasharray={index === 1 ? "5 3" : index === 2 ? "1 3" : undefined}
            className={styles[index]}
          />
        ) : null,
      )}
    </svg>
  );
}

function EquityCurve({ points }: { points: { ms: number; balance: number }[] }) {
  if (points.length < 2)
    return <p className="text-sm text-muted-foreground">No ledger entries yet.</p>;
  const width = 480;
  const height = 160;
  const xs = points.map((p) => p.ms);
  const ys = points.map((p) => p.balance);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const path = points
    .map(
      (p, i) =>
        `${i ? "L" : "M"}${((p.ms - x0) / (x1 - x0 || 1)) * width},${height - ((p.balance - y0) / (y1 - y0 || 1)) * height}`,
    )
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="h-40 w-full text-primary"
      role="img"
      aria-label="Paper wallet equity"
    >
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}
