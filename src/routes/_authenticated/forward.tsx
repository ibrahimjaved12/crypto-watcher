import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { useMemo, useState } from "react";
import { FlaskConical, PlayCircle } from "lucide-react";
import { toast } from "sonner";
import { AppShell } from "@/components/app-shell";
import {
  EmptyState,
  ErrorNotice,
  PageHeader,
  StatusBadge,
  TechnicalDetails,
} from "@/components/presentation";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { summarizeOutcomes } from "@/lib/forward/forward-dashboard";
import {
  explainSignalEngine,
  explainTrendTrack,
  type StatusLine,
} from "@/lib/forward/forward-status";
import {
  benchmarkLabel,
  issueSummary,
  monitoringRunLabel,
  pairLabel,
  sampleLabel,
  strategyLabel,
  trendVariantLabel,
} from "@/lib/presentation/labels";
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
  ms == null ? "—" : new Date(ms).toISOString().slice(0, 16).replace("T", " ");
const day = (ms: number | null | undefined) =>
  ms == null ? "—" : new Date(ms).toISOString().slice(0, 10);
const r = (value: number | null) => (value === null ? "—" : value.toFixed(3));
const pct = (value: number | null, digits = 3) =>
  value === null ? "—" : `${(value * 100).toFixed(digits)}%`;

function ForwardPage() {
  return (
    <AppShell>
      <PageHeader
        title="Strategy Lab"
        description="Observe signals, track paper trades and compare hypothetical portfolios over time."
      />
      <div className="mb-6 mt-5 flex items-start gap-3 rounded-xl border border-warn/30 bg-warn/5 p-4 text-sm">
        <FlaskConical className="mt-0.5 size-5 shrink-0 text-warn" aria-hidden />
        <p>
          <span className="font-medium text-warn">Experimental forward evaluation.</span> No
          strategy currently has a validated trading edge.
        </p>
      </div>
      <Tabs defaultValue="signals">
        <TabsList
          className="mb-5 h-auto w-full justify-start gap-1 overflow-x-auto p-1 sm:w-auto"
          aria-label="Strategy Lab views"
        >
          <TabsTrigger className="py-2.5" value="signals">
            Signals & paper trading
          </TabsTrigger>
          <TabsTrigger className="py-2.5" value="portfolios">
            Daily portfolios
          </TabsTrigger>
        </TabsList>
        <TabsContent value="signals">
          <SignalsSection />
        </TabsContent>
        <TabsContent value="portfolios">
          <TrendSection />
        </TabsContent>
      </Tabs>
    </AppShell>
  );
}

function SystemStatus({
  line,
  updated,
  updatedLabel = "Evaluation boundary",
}: {
  line: StatusLine;
  updated?: string | undefined;
  updatedLabel?: string;
}) {
  return (
    <section
      className="panel flex flex-wrap items-start justify-between gap-4 p-5"
      aria-label="System status"
    >
      <div>
        <p className="eyebrow mb-2">System status</p>
        <StatusBadge
          tone={line.level === "ok" ? "good" : line.level === "wait" ? "neutral" : "warning"}
        >
          {line.title}
        </StatusBadge>
        <p className="mt-3 max-w-3xl text-sm leading-relaxed text-muted-foreground">
          {line.detail}
        </p>
      </div>
      {updated && (
        <p className="text-xs text-muted-foreground">
          {updatedLabel} <span className="num">{updated} UTC</span>
        </p>
      )}
    </section>
  );
}

function SignalsSection() {
  const queryClient = useQueryClient();
  const load = useServerFn(getForwardDashboard);
  const run = useServerFn(runForwardNow);
  const [sinceDays, setSinceDays] = useState(30);
  const data = useQuery({ queryKey: ["forward-dashboard"], queryFn: () => load() });
  const runNow = useMutation({
    mutationFn: () => run(),
    onSuccess: (summary) => {
      toast(
        `${monitoringRunLabel(summary.status)}${summary.reason ? ` · ${issueSummary(summary.reason)}` : ""}`,
      );
      void queryClient.invalidateQueries({ queryKey: ["forward-dashboard"] });
    },
    onError: (error) =>
      toast.error(issueSummary(error instanceof Error ? error.message : String(error))),
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
  const equity = data.data?.equity ?? [];
  const positions = Object.entries(latest?.wallet_state?.positions ?? {});
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold">Signals & paper trading</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Completed-candle setups and simulated trade outcomes. All times below are UTC.
          </p>
        </div>
        <Button onClick={() => runNow.mutate()} disabled={runNow.isPending}>
          <PlayCircle className="size-4" aria-hidden />
          {runNow.isPending ? "Evaluating signals…" : "Evaluate signals now"}
        </Button>
      </div>
      {runNow.error && <ErrorNotice error={runNow.error} />}
      {data.isPending ? (
        <p role="status" className="panel p-5 text-sm text-muted-foreground">
          Loading signal evaluation…
        </p>
      ) : data.error ? (
        <ErrorNotice title="Signal results unavailable." error={data.error} />
      ) : (
        <>
          <SystemStatus
            line={explainSignalEngine(latest, Date.now())}
            updated={latest ? time(latest.boundary_ms) : undefined}
          />
          <section className="grid gap-5 lg:grid-cols-2">
            <div className="panel p-5">
              <h3 className="mb-4 text-lg font-semibold">Latest signals</h3>
              {data.data?.signals.length ? (
                <ul className="divide-y divide-border/60">
                  {data.data.signals.slice(0, 20).map((signal) => (
                    <li key={signal.signal_id} className="py-3 first:pt-0">
                      <div className="flex items-center justify-between gap-3">
                        <span className="font-medium">{pairLabel(signal.symbol)}</span>
                        <StatusBadge tone={signal.side === 1 ? "good" : "warning"}>
                          {signal.side === 1 ? "Long" : "Short"}
                        </StatusBadge>
                      </div>
                      <p className="mt-1 text-sm">{strategyLabel(signal.strategy_id)}</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {time(signal.signal_ms)} UTC
                      </p>
                      <TechnicalDetails className="mt-2">
                        <p>
                          Strategy: <code>{signal.strategy_id}</code> · Version:{" "}
                          <code>{signal.version}</code>
                        </p>
                        <p>
                          Signal: <code>{signal.signal_id}</code> · Horizon: {signal.horizon_min}{" "}
                          minutes
                        </p>
                      </TechnicalDetails>
                    </li>
                  ))}
                </ul>
              ) : (
                <EmptyState title="No signals recorded">
                  Signals appear when a strategy's conditions are met on completed candles.
                </EmptyState>
              )}
            </div>
            <div className="panel p-5">
              <h3 className="mb-4 text-lg font-semibold">Open setups</h3>
              {data.data?.openSetups.length ? (
                <ul className="divide-y divide-border/60">
                  {data.data.openSetups.slice(0, 20).map((setup) => (
                    <li key={setup.setup_id} className="py-3 first:pt-0">
                      <div className="flex items-center justify-between gap-3">
                        <span className="font-medium">{pairLabel(setup.symbol)}</span>
                        <StatusBadge tone={setup.side === 1 ? "good" : "warning"}>
                          {setup.side === 1 ? "Long" : "Short"}
                        </StatusBadge>
                      </div>
                      <p className="mt-1 text-sm">{strategyLabel(setup.strategy_id)}</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        Reward:risk {setup.rr} · {time(setup.entry_ms)} UTC
                      </p>
                      <TechnicalDetails className="mt-2">
                        <p>
                          Strategy: <code>{setup.strategy_id}</code> · Version:{" "}
                          <code>{setup.version}</code>
                        </p>
                        <p>
                          Setup: <code>{setup.setup_id}</code> · Status: <code>{setup.status}</code>{" "}
                          · Horizon: {setup.horizon_min} minutes
                        </p>
                      </TechnicalDetails>
                    </li>
                  ))}
                </ul>
              ) : (
                <EmptyState title="No open setups">
                  Qualifying signals can create setups to evaluate. An empty list does not by itself
                  mean the system has failed.
                </EmptyState>
              )}
            </div>
          </section>
          <section className="panel min-w-0 p-5">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <h3 className="text-lg font-semibold">Strategy outcome comparison</h3>
              <select
                aria-label="Outcome history period"
                className="rounded-lg border bg-background p-2 text-sm"
                value={sinceDays}
                onChange={(e) => setSinceDays(Number(e.target.value))}
              >
                {[7, 30, 90, 365].map((days) => (
                  <option key={days} value={days}>
                    Last {days} days
                  </option>
                ))}
              </select>
            </div>
            <p className="mb-4 text-xs leading-relaxed text-muted-foreground">
              R is the return measured in units of initial trade risk. Average net R includes
              modeled costs. Controls provide a comparison; neither a positive average nor a small
              sample establishes an edge. Ambiguous outcomes use the pessimistic result.
            </p>
            {!summary.length ? (
              <EmptyState title="Awaiting completed trades">
                Final outcomes will appear here with matched control results.
              </EmptyState>
            ) : (
              <div
                className="overflow-x-auto"
                tabIndex={0}
                role="region"
                aria-label="Strategy outcomes"
              >
                <table className="data-table min-w-[900px]">
                  <caption className="sr-only">
                    Final strategy outcomes and matched controls
                  </caption>
                  <thead>
                    <tr>
                      {[
                        "Strategy",
                        "Trades",
                        "Reward:risk",
                        "Wins",
                        "Ambiguous",
                        "Average net R",
                        "Control trades",
                        "Control avg. R",
                      ].map((h) => (
                        <th scope="col" key={h}>
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {summary.map((row) => (
                      <tr key={`${row.strategyId}|${row.version}|${row.rr}`}>
                        <td>
                          <span className="font-medium">{strategyLabel(row.strategyId)}</span>
                          <TechnicalDetails className="mt-2">
                            <p>
                              ID: <code>{row.strategyId}</code> · Version:{" "}
                              <code>{row.version}</code>
                            </p>
                            <p>
                              Matched control: <code>placebo-v1:{row.strategyId}</code>
                            </p>
                            <p>Score band: unavailable (no score recorded).</p>
                          </TechnicalDetails>
                        </td>
                        <td className="num">{row.n}</td>
                        <td className="num">{row.rr}</td>
                        <td className="num">{row.wins}</td>
                        <td className="num">{row.ambiguous}</td>
                        <td className="num">{r(row.meanNetR)}</td>
                        <td className="num">{row.placebo?.n ?? 0}</td>
                        <td className="num">{r(row.placebo?.meanNetR ?? null)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <section className="grid gap-5 lg:grid-cols-2">
            <div className="panel p-5">
              <h3 className="text-lg font-semibold">Paper wallet</h3>
              <p className="mb-4 mt-1 text-xs text-muted-foreground">
                Realized balance · USDT · simulated trades
              </p>
              {equity.length > 0 && (
                <p className="num mb-4 text-2xl">
                  {equity[equity.length - 1]!.balance.toLocaleString(undefined, {
                    maximumFractionDigits: 2,
                  })}{" "}
                  <span className="text-sm text-muted-foreground">USDT</span>
                </p>
              )}
              <EquityCurve points={equity} />
            </div>
            <div className="panel p-5">
              <h3 className="mb-4 text-lg font-semibold">Open paper positions</h3>
              {positions.length ? (
                <ul className="divide-y divide-border/60">
                  {positions.map(([id, position]) => (
                    <li key={id} className="flex flex-wrap justify-between gap-3 py-3 text-sm">
                      <span className="font-medium">{pairLabel(String(position["symbol"]))}</span>
                      <span>
                        {position["side"] === 1 ? "Long" : "Short"} · Leverage{" "}
                        {String(position["leverage"])}×
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <EmptyState title="No paper positions open">
                  Simulated positions will be listed here when available.
                </EmptyState>
              )}
            </div>
          </section>
          <TechnicalDetails title="Signal engine technical details">
            <pre className="whitespace-pre-wrap">{JSON.stringify(latest, null, 2)}</pre>
            <p>
              Local development: <code>npm run dev:local:all</code> starts the application with its
              collector. Strategies can require up to 260 completed candles per timeframe.
            </p>
            {runNow.data && (
              <pre className="whitespace-pre-wrap">{JSON.stringify(runNow.data, null, 2)}</pre>
            )}
          </TechnicalDetails>
        </>
      )}
    </div>
  );
}

function TrendSection() {
  const queryClient = useQueryClient();
  const load = useServerFn(getTrendDashboard);
  const run = useServerFn(runForwardTrendNow);
  const [sample, setSample] = useState<"prospective" | "retrospective">("prospective");
  const [selected, setSelected] = useState("");
  const [showObservations, setShowObservations] = useState(false);
  const data = useQuery({ queryKey: ["forward-trend-dashboard"], queryFn: () => load() });
  const runNow = useMutation({
    mutationFn: () => run(),
    onSuccess: (summary) => {
      toast(
        `${monitoringRunLabel(summary.status)}${summary.reason ? ` · ${issueSummary(summary.reason)}` : ""}`,
      );
      void queryClient.invalidateQueries({ queryKey: ["forward-trend-dashboard"] });
    },
    onError: (error) =>
      toast.error(issueSummary(error instanceof Error ? error.message : String(error))),
  });
  const tracks =
    (sample === "prospective" ? data.data?.tracks : data.data?.reconstructedTracks) ?? [];
  const byName = new Map(tracks.map((track) => [track.track, track]));
  const variants = tracks.filter((track) => track.kind === "variant");
  const active =
    variants.find((track) => track.track === selected) ??
    variants.find((track) => track.equity.length >= 2) ??
    variants[0];
  const control = active?.control ? byName.get(active.control) : undefined;
  const equalWeight = byName.get("ew_long");
  const latest = data.data?.latestRun;
  const hasCurves = variants.some((track) => track.equity.length >= 2);
  return (
    <section className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold">Daily Trend Portfolios</h2>
          <p className="mt-1 max-w-3xl text-sm leading-relaxed text-muted-foreground">
            Hypothetical open-to-close returns. A decision recorded before the daily close may have
            been recorded after the open; these are not opening-price executions.
          </p>
        </div>
        <Button onClick={() => runNow.mutate()} disabled={runNow.isPending}>
          <PlayCircle className="size-4" aria-hidden />
          {runNow.isPending ? "Evaluating portfolios…" : "Evaluate daily portfolios"}
        </Button>
      </div>
      <details className="rounded-lg border border-border p-4 text-sm">
        <summary className="cursor-pointer font-medium">How to read these portfolios</summary>
        <div className="mt-3 space-y-2 leading-relaxed text-muted-foreground">
          <p>
            Each fixed variant is a hypothetical volatility-targeted portfolio. Returns include 11
            basis points per unit turnover and funding, with no margin, liquidation or position
            limits. No variant has a validated edge.
          </p>
          <p>
            “Recorded before daily close” preserves the prospective sample: recorded before close
            does not necessarily mean recorded before the open. “Reconstructed after the close” is
            historical reconstruction, not forward evidence. Samples remain separate.
          </p>
          <p>
            Exposure weights are multiples of portfolio value. Positive means long, negative means
            short, and zero means flat. They can exceed 1 in absolute magnitude.
          </p>
        </div>
      </details>
      {runNow.error && <ErrorNotice error={runNow.error} />}
      {data.isPending ? (
        <p role="status" className="panel p-5 text-sm text-muted-foreground">
          Loading daily portfolio results…
        </p>
      ) : data.error ? (
        <ErrorNotice title="Portfolio results unavailable." error={data.error} />
      ) : (
        <>
          <SystemStatus
            line={explainTrendTrack(
              data.data?.latestAttempt ?? latest,
              Math.max(0, ...(data.data?.tracks ?? []).map((track) => track.prospectiveDays)),
              (data.data?.tracks ?? []).some(
                (track) => track.kind === "variant" && track.currentWeights,
              ),
            )}
            updated={latest?.through_day_ms != null ? day(latest.through_day_ms) : undefined}
            updatedLabel="Results through"
          />
          <div className="flex flex-wrap items-center gap-3">
            <label className="text-sm font-medium" htmlFor="trend-sample">
              Evaluation sample
            </label>
            <select
              id="trend-sample"
              className="rounded-lg border bg-background p-2 text-sm"
              value={sample}
              onChange={(e) => setSample(e.target.value as typeof sample)}
            >
              <option value="prospective">Recorded before daily close</option>
              <option value="retrospective">Reconstructed after the close</option>
            </select>
          </div>
          <p className="text-xs text-muted-foreground">
            {sample === "prospective"
              ? "Before-close recording can happen after the open. Returns remain hypothetical."
              : "Historical reconstruction only. These observations are not forward evidence."}
          </p>
          {variants.length > 0 && (
            <div
              className="panel overflow-x-auto"
              tabIndex={0}
              role="region"
              aria-label="Daily portfolio comparison"
            >
              <table className="data-table min-w-[1100px]">
                <caption className="sr-only">
                  Daily portfolio statistics — {sampleLabel(sample)}
                </caption>
                <thead>
                  <tr>
                    {[
                      "Portfolio variant",
                      "Days in sample",
                      "Average daily return",
                      "Buy & hold benchmark",
                      "Equal-weight benchmark",
                      "Average daily turnover",
                      "Gross exposure",
                      "Evidence / sample size",
                    ].map((h) => (
                      <th scope="col" key={h}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {variants.map((track) => {
                    const matched = track.control ? byName.get(track.control) : undefined;
                    return (
                      <tr
                        key={track.track}
                        className={active?.track === track.track ? "bg-primary/5" : ""}
                      >
                        <td className="min-w-64">
                          <button
                            className="text-left font-medium text-primary underline-offset-4 hover:underline"
                            onClick={() => setSelected(track.track)}
                            aria-pressed={active?.track === track.track}
                          >
                            {trendVariantLabel(track.track)}
                          </button>
                          <p className="mt-2 text-xs text-muted-foreground">
                            Select to view exposure & equity
                          </p>
                        </td>
                        <td className="num">
                          {track.days}
                          <details className="mt-2 font-sans text-xs text-muted-foreground">
                            <summary className="cursor-pointer">Both samples</summary>
                            <p className="mt-2">
                              Before close: {track.prospectiveDays}
                              <br />
                              Reconstructed: {track.retrospectiveDays}
                            </p>
                          </details>
                        </td>
                        <td className="num">{pct(track.meanDaily)}</td>
                        <td className="num">{pct(matched?.meanDaily ?? null)}</td>
                        <td className="num">{pct(equalWeight?.meanDaily ?? null)}</td>
                        <td className="num">
                          {track.meanTurnover === null ? "—" : `${track.meanTurnover.toFixed(3)}×`}
                        </td>
                        <td className="num">
                          {track.meanGross === null ? "—" : `${track.meanGross.toFixed(2)}×`}
                        </td>
                        <td className="min-w-56 text-xs">
                          <p>
                            {track.muMinDaily === null
                              ? sample === "prospective"
                                ? "Not enough forward observations yet"
                                : "Not enough reconstructed observations yet"
                              : `Detection threshold: ${pct(track.muMinDaily)} per day`}
                          </p>
                          <details className="mt-2 text-muted-foreground">
                            <summary className="cursor-pointer">Statistical context</summary>
                            <p className="mt-2 leading-relaxed">{track.edgeLine}</p>
                            <p className="mt-2">
                              Normal approximation, independent daily returns, two-sided α=0.05 and
                              80% power. This estimates the sample's ability to detect an edge; it
                              is not a significance verdict.
                            </p>
                          </details>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <section className="panel space-y-5 p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h3 className="text-lg font-semibold">Portfolio detail</h3>
              {variants.length > 0 && (
                <select
                  aria-label="Portfolio variant to inspect"
                  className="w-full rounded-lg border bg-background p-2 text-sm sm:max-w-lg"
                  value={active?.track ?? ""}
                  onChange={(e) => setSelected(e.target.value)}
                >
                  {variants.map((track) => (
                    <option key={track.track} value={track.track}>
                      {trendVariantLabel(track.track)}
                    </option>
                  ))}
                </select>
              )}
            </div>
            {!hasCurves ? (
              <EmptyState title="Equity comparison is waiting for daily results">
                At least two observations in the selected sample are needed to draw a curve.
                Recorded portfolio weights can appear before scored returns.
              </EmptyState>
            ) : active && active.equity.length >= 2 ? (
              <>
                <p className="text-sm text-muted-foreground">
                  {sampleLabel(sample)} · Hypothetical equity multiple ·{" "}
                  {day(active.equity[0]?.day_ms)} to{" "}
                  {day(active.equity[active.equity.length - 1]?.day_ms)} UTC
                </p>
                <MultiCurve
                  series={[active.equity, control?.equity ?? [], equalWeight?.equity ?? []]}
                />
                <ul className="space-y-2 text-xs">
                  <li className="text-primary">
                    Solid line · {trendVariantLabel(active.track)} · Latest{" "}
                    {active.equity[active.equity.length - 1]!.equity.toFixed(3)}×
                  </li>
                  <li className="text-muted-foreground">
                    Dashed line ·{" "}
                    {active.control ? benchmarkLabel(active.control) : "Matched buy & hold control"}
                    {control?.equity.length
                      ? ` · Latest ${control.equity[control.equity.length - 1]!.equity.toFixed(3)}×`
                      : " · No observations"}
                  </li>
                  <li className="text-warn">
                    Dotted line · {benchmarkLabel("ew_long")}
                    {equalWeight?.equity.length
                      ? ` · Latest ${equalWeight.equity[equalWeight.equity.length - 1]!.equity.toFixed(3)}×`
                      : " · No observations"}
                  </li>
                </ul>
                <details
                  className="text-xs"
                  onToggle={(event) => setShowObservations(event.currentTarget.open)}
                >
                  <summary className="cursor-pointer text-muted-foreground">
                    Equity observations · latest 30
                  </summary>
                  {showObservations && (
                    <div className="mt-3 max-h-72 overflow-auto">
                      <table className="data-table">
                        <thead>
                          <tr>
                            <th scope="col">Date (UTC)</th>
                            <th scope="col">Variant</th>
                            <th scope="col">Buy & hold</th>
                            <th scope="col">Equal-weight</th>
                          </tr>
                        </thead>
                        <tbody>
                          {active.equity.slice(-30).map((point) => (
                            <tr key={point.day_ms}>
                              <td>{day(point.day_ms)}</td>
                              <td className="num">{point.equity.toFixed(4)}×</td>
                              <td className="num">
                                {control?.equity
                                  .find((p) => p.day_ms === point.day_ms)
                                  ?.equity.toFixed(4) ?? "—"}
                              </td>
                              <td className="num">
                                {equalWeight?.equity
                                  .find((p) => p.day_ms === point.day_ms)
                                  ?.equity.toFixed(4) ?? "—"}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </details>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                This variant needs at least two observations. Select another variant to compare
                equity.
              </p>
            )}
            {active && (
              <div className="border-t border-border pt-5">
                <h3 className="font-semibold">Latest recorded exposure</h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  Latest saved weights, independent of the selected return sample. Positive = long ·
                  Negative = short · Zero = flat. Values are exposure multiples, not percentages.
                </p>
                {active.currentWeights ? (
                  <>
                    <p className="mt-3 text-xs text-muted-foreground">
                      For {day(active.currentWeights.day_ms)} ·{" "}
                      {sampleLabel(active.currentWeights.sample_kind)}
                    </p>
                    <div className="my-3 flex flex-wrap gap-2">
                      {Object.entries(active.currentWeights.weights).map(([symbol, weight]) => (
                        <div
                          key={symbol}
                          className="rounded-lg border border-border bg-background/50 px-3 py-2"
                        >
                          <span className="mr-3 text-sm font-medium">
                            {symbol.replace(/USDT$/, "")}
                          </span>
                          <span
                            className={`num text-sm ${weight > 0 ? "text-bull" : weight < 0 ? "text-bear" : "text-muted-foreground"}`}
                          >
                            {weight > 0 ? "+" : ""}
                            {weight.toFixed(2)}×
                          </span>
                          <span className="ml-2 text-xs text-muted-foreground">
                            {weight > 0 ? "Long" : weight < 0 ? "Short" : "Flat"}
                          </span>
                        </div>
                      ))}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Decided {time(active.currentWeights.decided_at_ms)} UTC · Recorded{" "}
                      {time(active.currentWeights.recorded_at_ms)} UTC
                    </p>
                  </>
                ) : (
                  <p className="mt-3 text-sm text-muted-foreground">
                    No exposure weights recorded for this variant yet.
                  </p>
                )}
                {active.control && (
                  <p className="mt-3 text-xs text-muted-foreground">
                    Matched control: {benchmarkLabel(active.control)}
                  </p>
                )}
                <TechnicalDetails className="mt-4">
                  <p>
                    Variant: <code>{active.track}</code> · Control:{" "}
                    <code>{active.control ?? "none"}</code> · Sample: <code>{sample}</code>
                  </p>
                  <p>Daily standard deviation: {pct(active.sdDaily)}</p>
                  {active.currentWeights && (
                    <pre className="whitespace-pre-wrap">
                      {JSON.stringify(active.currentWeights, null, 2)}
                    </pre>
                  )}
                </TechnicalDetails>
              </div>
            )}
          </section>
          <TechnicalDetails title="Portfolio evaluation technical details">
            <p>
              Latest committed run: <code>{latest?.status ?? "none"}</code> ·{" "}
              <code>{latest?.run_key}</code>
            </p>
            <p className="whitespace-pre-wrap">{latest?.reason ?? "No committed run reason."}</p>
            <pre className="whitespace-pre-wrap">
              {JSON.stringify(data.data?.latestAttempt, null, 2)}
            </pre>
            {runNow.data && (
              <pre className="whitespace-pre-wrap">{JSON.stringify(runNow.data, null, 2)}</pre>
            )}
          </TechnicalDetails>
        </>
      )}
    </section>
  );
}

function MultiCurve({ series }: { series: { day_ms: number; equity: number }[][] }) {
  const points = series.flat();
  const width = 800,
    height = 200;
  const xs = points.map((p) => p.day_ms),
    ys = points.map((p) => p.equity);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const path = (line: { day_ms: number; equity: number }[]) =>
    line
      .map(
        (p, i) =>
          `${i ? "L" : "M"}${((p.day_ms - x0) / (x1 - x0 || 1)) * width},${height - ((p.equity - y0) / (y1 - y0 || 1)) * height}`,
      )
      .join(" ");
  const styles = ["text-primary", "text-muted-foreground", "text-warn"];
  return (
    <div>
      <div className="num mb-2 flex justify-between text-xs text-muted-foreground">
        <span>
          Range {y0.toFixed(3)}× – {y1.toFixed(3)}×
        </span>
        <span>Equity multiple</span>
      </div>
      <svg
        viewBox={`-4 -4 ${width + 8} ${height + 8}`}
        className="h-52 w-full"
        role="img"
        aria-label="Hypothetical portfolio equity versus matched buy and hold and equal-weight benchmarks. Exact values are available in Equity observations."
      >
        {series.map((line, index) =>
          line.length > 1 ? (
            <path
              key={index}
              d={path(line)}
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeDasharray={index === 1 ? "8 5" : index === 2 ? "2 5" : undefined}
              className={styles[index]}
            />
          ) : null,
        )}
      </svg>
    </div>
  );
}

function EquityCurve({ points }: { points: { ms: number; balance: number }[] }) {
  if (points.length < 2)
    return (
      <EmptyState title="Awaiting paper wallet history">
        At least two ledger entries are needed to show realized balance over time.
      </EmptyState>
    );
  const width = 480,
    height = 160;
  const xs = points.map((p) => p.ms),
    ys = points.map((p) => p.balance);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const path = points
    .map(
      (p, i) =>
        `${i ? "L" : "M"}${((p.ms - x0) / (x1 - x0 || 1)) * width},${height - ((p.balance - y0) / (y1 - y0 || 1)) * height}`,
    )
    .join(" ");
  return (
    <>
      <p className="num mb-3 text-xs text-muted-foreground">
        {y0.toFixed(2)}–{y1.toFixed(2)} USDT · {day(x0)} to {day(x1)}
      </p>
      <svg
        viewBox={`-3 -3 ${width + 6} ${height + 6}`}
        className="h-40 w-full text-primary"
        role="img"
        aria-label={`Realized paper wallet balance, ${day(x0)} to ${day(x1)}, range ${y0.toFixed(2)} to ${y1.toFixed(2)} USDT`}
      >
        <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" />
      </svg>
    </>
  );
}
