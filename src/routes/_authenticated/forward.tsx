import { PageHeader, TechnicalDetails, EmptyState, QueryNotice } from "@/components/presentation";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  monitoringRunLabel,
  strategyLabel,
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
  ms ? new Date(ms).toISOString().slice(0, 16).replace("T", " ") : "—";
const r = (value: number | null) => (value === null ? "—" : value.toFixed(3));

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
          description="Follow experimental strategies as new market observations arrive."
        />
        <div className="rounded-xl border border-warn/40 bg-warn/10 p-4 text-sm text-warn">
          Experimental forward evaluation. No strategy currently has a validated trading edge.
        </div>
        <StatusSummary signalRun={latest} signalPending={data.isPending} signalError={data.error} />
        <Tabs defaultValue="signals">
          <TabsList className="mb-5 h-auto flex-wrap">
            <TabsTrigger value="signals">Signals &amp; paper trading</TabsTrigger>
            <TabsTrigger value="portfolios">Daily portfolios</TabsTrigger>
          </TabsList>
          <TabsContent value="signals" className="space-y-6">
            <div className="flex flex-wrap items-center gap-3">
              <div>
                <h2 className="text-xl font-semibold">Signals &amp; paper trading</h2>
                <p className="mt-1 text-xs text-muted-foreground">
                  Completed-candle setups and a simulated wallet. Times shown in UTC.
                </p>
              </div>
              <Badge variant={fresh ? "secondary" : "outline"}>
                {latest
                  ? `${fresh ? "Fresh" : "Delayed"} · ${monitoringRunLabel(latest.status)} · ${time(latest.boundary_ms)} UTC`
                  : "No evaluations yet"}
              </Badge>
              <Button
                className="sm:ml-auto"
                size="sm"
                onClick={() => runNow.mutate()}
                disabled={runNow.isPending}
              >
                {runNow.isPending ? "Evaluating…" : "Evaluate signals now"}
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
              <TechnicalDetails>
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
                <h2 className="font-medium">Strategy outcome comparison</h2>
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
                R is the result relative to a trade’s initial risk. +1 R earns that amount; −1 R
                loses it. Net R includes modeled costs. Controls provide a comparison, not evidence
                of a validated edge.
              </p>
              <div
                className="overflow-x-auto"
                tabIndex={0}
                role="region"
                aria-label="Strategy outcomes"
              >
                <table className="data-table">
                  <thead>
                    <tr className="text-left text-muted-foreground">
                      <th>Trades</th>
                      <th>Strategy</th>
                      <th>Reward:risk</th>
                      <th>Wins</th>
                      <th>Ambiguous</th>
                      <th>Average net R</th>
                      <th>Control trades</th>
                      <th>Control avg. R</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.map((row) => (
                      <tr key={`${row.strategyId}|${row.version}|${row.rr}`} className="border-t">
                        <td>{row.n}</td>
                        <td className="min-w-48">
                          {strategyLabel(row.strategyId)}
                          <TechnicalDetails>
                            <p>
                              {row.strategyId} · {row.version}
                            </p>
                            <p>Score band: unavailable (no score yet)</p>
                          </TechnicalDetails>
                        </td>
                        <td>{row.rr}</td>
                        <td>{row.wins}</td>
                        <td>{row.ambiguous}</td>
                        <td>{r(row.meanNetR)}</td>
                        <td>{row.placebo?.n ?? 0}</td>
                        <td>{r(row.placebo?.meanNetR ?? null)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!data.isPending && !data.error && summary.length === 0 && (
                <EmptyState title="No completed trades in this period">
                  Outcome comparisons appear as setups finish. Try a longer period to include
                  earlier results.
                </EmptyState>
              )}
            </section>

            <section className="grid gap-4 md:grid-cols-2">
              <div className="panel p-5">
                <h2 className="mb-3 font-semibold">Latest signals</h2>
                <ul className="space-y-1 text-sm">
                  {!data.isPending && !data.error && !data.data?.signals.length && (
                    <li className="text-muted-foreground">
                      No signals recorded yet. Evaluate signals when market history is ready.
                    </li>
                  )}
                  {(data.data?.signals ?? []).slice(0, 20).map((signal) => (
                    <li
                      className="border-b border-border/60 py-3 last:border-0"
                      key={signal.signal_id}
                    >
                      {time(signal.signal_ms)} · {pairLabel(signal.symbol)} ·{" "}
                      {signal.side === 1 ? "long" : "short"} · {strategyLabel(signal.strategy_id)}
                      <TechnicalDetails>
                        {signal.strategy_id} · {signal.version}
                      </TechnicalDetails>
                    </li>
                  ))}
                </ul>
              </div>
              <div className="panel p-5">
                <h2 className="mb-3 font-semibold">Open setups</h2>
                <ul className="space-y-1 text-sm">
                  {!data.isPending && !data.error && !data.data?.openSetups.length && (
                    <li className="text-muted-foreground">
                      No open setups. Qualifying signals will appear here.
                    </li>
                  )}
                  {(data.data?.openSetups ?? []).slice(0, 20).map((setup) => (
                    <li
                      className="border-b border-border/60 py-3 last:border-0"
                      key={setup.setup_id}
                    >
                      {time(setup.entry_ms)} · {pairLabel(setup.symbol)} ·{" "}
                      {setup.side === 1 ? "long" : "short"} · {strategyLabel(setup.strategy_id)} ·
                      Reward:risk {setup.rr}
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
                <h2 className="mb-3 font-semibold">Paper wallet equity (USDT, realized)</h2>
                {!data.isPending && !data.error && <EquityCurve points={equity} />}
              </div>
              <div className="panel p-5">
                <h2 className="mb-3 font-semibold">Open paper positions</h2>
                <ul className="space-y-1 text-sm">
                  {positions.map(([id, position]) => (
                    <li key={id}>
                      {pairLabel(String(position["symbol"]))} ·{" "}
                      {position["side"] === 1 ? "long" : "short"} · leverage{" "}
                      {String(position["leverage"])}x
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
const day = (ms: number | null | undefined) =>
  ms === null || ms === undefined ? "—" : new Date(ms).toISOString().slice(0, 10);

const dot = { ok: "bg-green-500", wait: "bg-amber-500", problem: "bg-red-500" } as const;

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
          title: "Signal engine",
          detail: "Status unavailable. Refresh to try again.",
        }
      : signalPending
        ? { level: "wait", title: "Signal engine", detail: "Loading status…" }
        : explainSignalEngine(signalRun, Date.now()),
    trend.error
      ? {
          level: "problem",
          title: "Daily portfolios",
          detail: "Status unavailable. Refresh to try again.",
        }
      : trend.isPending
        ? { level: "wait", title: "Daily portfolios", detail: "Loading status…" }
        : explainTrendTrack(
            trend.data?.latestAttempt ?? trend.data?.latestRun,
            Math.max(0, ...tracks.map((track) => track.prospectiveDays)),
            tracks.some((track) => track.kind === "variant" && track.currentWeights),
          ),
  ];
  return (
    <div className="panel space-y-3 p-5 text-sm">
      <div className="font-medium">System status</div>
      {lines.map((line) => (
        <div key={line.title} className="flex gap-2">
          <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${dot[line.level]}`} />
          <div>
            <div className="font-medium">
              {line.title}{" "}
              <span className="ml-2 text-xs text-muted-foreground">
                {line.level === "ok"
                  ? "Healthy"
                  : line.level === "wait"
                    ? "Waiting"
                    : "Needs attention"}
              </span>
            </div>
            <div className="text-muted-foreground">{line.detail}</div>
          </div>
        </div>
      ))}
      <TechnicalDetails title="Evaluation diagnostics">
        <p>
          Signal status: {signalRun?.status ?? "not run"} · {signalRun?.reason ?? "no reason"}
        </p>
        <p>
          Portfolio status:{" "}
          {trend.data?.latestAttempt?.status ?? trend.data?.latestRun?.status ?? "not run"} ·{" "}
          {trend.data?.latestAttempt?.reason ?? trend.data?.latestRun?.reason ?? "no reason"}
        </p>
        <p>
          Collector history may require up to 260 completed candles per timeframe. Local development
          command: <code>npm run dev:local:all</code>.
        </p>
        {trend.error && <p>{trend.error.message}</p>}
      </TechnicalDetails>
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
      <div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
        Hypothetical open-to-close portfolios, with 11 bp per unit turnover and funding but no
        margin, liquidation or position limits. No variant has a validated edge. “Recorded before
        daily close” can include decisions recorded after the open; it does not imply execution at
        the opening price. Reconstructed results are not forward evidence.
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="font-medium">Daily Trend Portfolios</h2>
        <Badge variant={!latest ? "outline" : latest.status === "ok" ? "secondary" : "destructive"}>
          {latest
            ? `${monitoringRunLabel(latest.status)} · through ${day(latest.through_day_ms)}`
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
          {runNow.isPending ? "Evaluating…" : "Evaluate daily portfolios"}
        </Button>
        <select
          aria-label="Trend sample"
          className="rounded border bg-background px-2 py-1 text-sm"
          value={sample}
          onChange={(event) => setSample(event.target.value as typeof sample)}
        >
          <option value="prospective">Recorded before daily close</option>
          <option value="retrospective">Reconstructed after the close</option>
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
      <TechnicalDetails>
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
        Statistics use the selected sample. Exposure weights are multiples of portfolio value:
        positive = long, negative = short, zero = flat. Weights can exceed 1×.
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
              <th>Variant</th>
              <th>Days: before close / reconstructed</th>
              <th>Average daily return</th>
              <th>Buy &amp; hold benchmark</th>
              <th>Equal-weight benchmark</th>
              <th>Average daily turnover</th>
              <th>Gross exposure</th>
              <th>Latest recorded weights</th>
              <th>Evidence / sample size</th>
            </tr>
          </thead>
          <tbody>
            {variants.map((track) => {
              const control = track.control ? byName.get(track.control) : undefined;
              return (
                <tr key={track.track} className="border-t align-top">
                  <td className="min-w-64 font-medium">
                    {trendVariantLabel(track.track)}
                    <TechnicalDetails>
                      <p>Variant: {track.track}</p>
                      <p>Control: {track.control ?? "none"}</p>
                      <p>Sample: {track.sampleKind}</p>
                    </TechnicalDetails>
                  </td>
                  <td>
                    {track.prospectiveDays} / {track.retrospectiveDays}
                  </td>
                  <td>{pct(track.meanDaily)}</td>
                  <td>
                    {pct(control?.meanDaily ?? null)}
                    <details className="mt-2 text-xs text-muted-foreground">
                      <summary>Matched benchmark</summary>
                      {track.control ? benchmarkLabel(track.control) : "Unavailable"}
                    </details>
                  </td>
                  <td>{pct(equalWeight?.meanDaily ?? null)}</td>
                  <td>{track.meanTurnover === null ? "—" : track.meanTurnover.toFixed(3)}</td>
                  <td>{track.meanGross === null ? "—" : `${track.meanGross.toFixed(2)}×`}</td>
                  <td className="text-xs">
                    {track.currentWeights ? (
                      <>
                        <p className="mb-2">{day(track.currentWeights.day_ms)}</p>
                        <div className="flex min-w-48 flex-wrap gap-1.5">
                          {Object.entries(track.currentWeights.weights).map(([symbol, weight]) => (
                            <span
                              key={symbol}
                              className={`num rounded-md border px-2 py-1 ${weight > 0 ? "text-bull" : weight < 0 ? "text-bear" : "text-muted-foreground"}`}
                            >
                              {symbol.replace(/USDT$/, "")} {weight > 0 ? "+" : ""}
                              {weight.toFixed(2)}×{" "}
                              <span className="font-sans">
                                {weight > 0 ? "Long" : weight < 0 ? "Short" : "Flat"}
                              </span>
                            </span>
                          ))}
                        </div>
                        <div>
                          Decided {time(track.currentWeights.decided_at_ms)} UTC; recorded{" "}
                          {time(track.currentWeights.recorded_at_ms)} UTC (
                          {sampleLabel(track.currentWeights.sample_kind)})
                        </div>
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="min-w-52 text-xs">
                    <p>
                      {track.muMinDaily === null
                        ? sample === "prospective"
                          ? "Not enough forward observations yet"
                          : "Not enough reconstructed observations yet"
                        : `${track.days} observations · detectable daily edge ${pct(track.muMinDaily)}`}
                    </p>
                    <details className="mt-2">
                      <summary>Statistical context</summary>
                      <p className="mt-2 text-muted-foreground">
                        {track.edgeLine}. This is a sample-size estimate, not a significance
                        verdict.
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
          Evaluate daily portfolios once completed market history is available. Scored returns
          appear after the daily close.
        </EmptyState>
      )}
      {!data.isPending &&
        !data.error &&
        (selected ? (
          <div className="panel space-y-4 p-5">
            <div className="flex flex-wrap items-center gap-3">
              <h3 className="font-semibold">Compare cumulative equity</h3>
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
          <EmptyState title="Equity comparison is waiting for observations">
            At least two scored days in this sample are needed to draw a curve. Switch samples to
            inspect reconstructed history separately.
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
