import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { useMemo, useState, type ReactNode } from "react";
import { ArrowDownRight, ArrowUpRight, FlaskConical, Layers3 } from "lucide-react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { Hint } from "@/components/hint";
import { Button } from "@/components/ui/button";
import { humanizeReason, outcomeLabel, strategyLabel } from "@/lib/labels";
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

const time = (ms: number | null | undefined) => (ms == null ? "—" : new Date(ms).toLocaleString());
const r = (value: number | null) =>
  value === null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(3)} R`;
const pct = (value: number | null, digits = 3) =>
  value === null ? "—" : `${value > 0 ? "+" : ""}${(value * 100).toFixed(digits)}%`;
const day = (ms: number | null | undefined) =>
  ms == null ? "—" : new Date(ms).toLocaleDateString(undefined, { timeZone: "UTC" });
const dot = { ok: "bg-bull", wait: "bg-warn", problem: "bg-bear" } as const;
const stateWord = { ok: "Working", wait: "Waiting", problem: "Needs attention" } as const;

function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-start gap-3 rounded-xl border border-dashed border-border p-5 text-sm text-muted-foreground">
      <FlaskConical className="mt-1 size-7 shrink-0 text-primary" aria-hidden />
      <p className="max-w-xl">{children}</p>
    </div>
  );
}

function StatusSummary({
  line,
  raw,
  updated,
}: {
  line: StatusLine;
  raw: string;
  updated?: string;
}) {
  return (
    <div className="rounded-xl border border-border bg-background/40 p-4 text-sm">
      <div className="flex items-center gap-2 font-medium">
        <span aria-hidden className={`size-2.5 shrink-0 rounded-full ${dot[line.level]}`} />
        {stateWord[line.level]}
      </div>
      <p className="mt-1">{line.detail}</p>
      {line.action && <p className="mt-1 text-muted-foreground">{line.action}</p>}
      <details className="mt-1 break-words text-xs text-muted-foreground">
        <summary>Technical details</summary>
        {updated && <p>Last run: {updated}</p>}
        <p className="num">{raw}</p>
        {line.technicalHelp && <p className="mt-2">{line.technicalHelp}</p>}
      </details>
    </div>
  );
}

function StrategyName({ id }: { id: string }) {
  const label = strategyLabel(id);
  return <span title={label.blurb}>{label.name}</span>;
}

function Direction({ side }: { side: unknown }) {
  return (
    <span className={`inline-flex items-center gap-1 ${side === 1 ? "text-bull" : "text-bear"}`}>
      {side === 1 ? (
        <ArrowUpRight className="size-3" aria-hidden />
      ) : (
        <ArrowDownRight className="size-3" aria-hidden />
      )}
      {side === 1 ? "Long" : "Short"}
    </span>
  );
}

function LoadError({ error }: { error: Error }) {
  return (
    <div role="alert" className="rounded-lg border border-bear/40 p-4 text-sm">
      <p className="text-bear">{humanizeReason(error.message).short}</p>
      <p className="mt-1 text-muted-foreground">
        Use the run button to retry the check and refresh these results.
      </p>
      <details className="break-words text-xs text-muted-foreground">
        <summary>Technical details</summary>
        {error.message}
      </details>
    </div>
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
      toast(humanizeReason(summary.reason || summary.status).short);
      void queryClient.invalidateQueries({ queryKey: ["forward-dashboard"] });
    },
    onError: (error) =>
      toast.error(
        humanizeReason(error instanceof Error ? error.message : "Signal check failed").short,
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
  const equity = data.data?.equity ?? [];
  const positions = Object.entries(latest?.wallet_state?.positions ?? {});
  const signals = (data.data?.signals ?? []).slice(0, 20);
  const setups = (data.data?.openSetups ?? []).slice(0, 20);

  return (
    <AppShell>
      <div className="min-w-0 space-y-6">
        <header>
          <h1 className="text-2xl font-semibold">Strategy Lab</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            Strategies are tested here with fake money and realistic fees. Nothing here is proven to
            make money yet.
          </p>
        </header>
        <div className="flex items-center gap-3 rounded-xl border border-warn/40 bg-warn/10 p-4 text-sm text-warn">
          <FlaskConical className="size-5 shrink-0" aria-hidden />
          No strategy has a validated edge; this is a forward test.
        </div>
        <section className="panel min-w-0 space-y-5 p-4 sm:p-6" aria-labelledby="signal-title">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 id="signal-title" className="text-xl font-semibold">
                Signals &amp; Paper Trading
              </h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Watch individual trade ideas play out, with costs included.
              </p>
            </div>
            <Button onClick={() => runNow.mutate()} disabled={runNow.isPending}>
              {runNow.isPending ? "Checking signals…" : "Check for new signals now"}
            </Button>
          </div>
          {runNow.error && <LoadError error={runNow.error} />}
          {data.isPending ? (
            <p role="status" className="text-sm text-muted-foreground">
              Loading signal results…
            </p>
          ) : data.error ? (
            <LoadError error={data.error} />
          ) : (
            <>
              <StatusSummary
                line={explainSignalEngine(latest, Date.now())}
                raw={
                  latest
                    ? `${latest.status} · ${latest.reason ?? "no reason reported"}`
                    : "No run recorded"
                }
                updated={time(latest?.boundary_ms)}
              />
              <section>
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <h3 className="font-semibold">Finished trades by strategy</h3>
                  <label className="flex items-center gap-2 text-xs text-muted-foreground">
                    Results from
                    <select
                      className="rounded-md border bg-background px-2 py-1 text-sm text-foreground"
                      value={sinceDays}
                      onChange={(event) => setSinceDays(Number(event.target.value))}
                    >
                      {[7, 30, 90, 365].map((days) => (
                        <option key={days} value={days}>
                          Last {days} days
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                {summary.length === 0 ? (
                  <EmptyState>
                    No finished trades yet in this period. The first results appear after a signal
                    fires and its time limit passes; this can take hours.
                  </EmptyState>
                ) : (
                  <div
                    className="overflow-x-auto"
                    tabIndex={0}
                    role="region"
                    aria-label="Finished trades by strategy"
                  >
                    <table className="w-full min-w-[840px] text-left text-xs">
                      <thead className="bg-secondary/40 text-muted-foreground">
                        <tr>
                          <th scope="col" className="p-3">
                            Trades
                          </th>
                          <th scope="col" className="p-3">
                            Strategy
                          </th>
                          <th scope="col" className="p-3">
                            <Hint term="reward/risk" />
                          </th>
                          <th
                            scope="col"
                            className="p-3"
                            title="Trades that reached their target; this is not a forecast of future wins."
                          >
                            Wins
                          </th>
                          <th scope="col" className="p-3" title={outcomeLabel("ambiguous")}>
                            Unclear results
                          </th>
                          <th scope="col" className="p-3">
                            Average result / trade (<Hint term="R" />)
                          </th>
                          <th scope="col" className="p-3">
                            <Hint term="placebo">Baseline comparison</Hint>
                          </th>
                          <th scope="col" className="p-3">
                            <Hint term="score band" />
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {summary.map((row) => (
                          <tr
                            key={`${row.strategyId}|${row.version}|${row.rr}`}
                            className="border-t align-top"
                          >
                            <td className="num p-3">{row.n}</td>
                            <td className="max-w-60 p-3">
                              <StrategyName id={row.strategyId} />
                              <details className="mt-1 text-muted-foreground">
                                <summary>Details</summary>
                                <p>{strategyLabel(row.strategyId).blurb}</p>
                                <p className="mt-1 break-all">
                                  {row.strategyId} · {row.version}
                                </p>
                              </details>
                            </td>
                            <td className="num p-3">{row.rr}</td>
                            <td className="num p-3">{row.wins}</td>
                            <td className="num p-3">{row.ambiguous}</td>
                            <td className="num p-3">{r(row.meanNetR)}</td>
                            <td className="p-3">
                              <span className="num">{r(row.placebo?.meanNetR ?? null)}</span>
                              <p className="mt-1 text-muted-foreground">
                                {row.placebo?.n ?? 0} random trades
                              </p>
                            </td>
                            <td className="p-3 text-muted-foreground">Not assigned yet</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <details className="mt-2 text-xs text-muted-foreground">
                  <summary>How to read trade outcomes</summary>
                  <p>{["T", "S", "E", "L", "X", "ambiguous"].map(outcomeLabel).join(" · ")}</p>
                  <p className="mt-2">
                    Unclear candles use the pessimistic result. A positive average in a small sample
                    is not evidence of future profit.
                  </p>
                </details>
              </section>
              <div className="grid min-w-0 gap-5 md:grid-cols-2">
                <section className="min-w-0">
                  <h3 className="mb-3 font-semibold">Latest signals</h3>
                  {signals.length ? (
                    <ul className="divide-y divide-border">
                      {signals.map((signal) => (
                        <li key={signal.signal_id} className="py-3 text-sm">
                          <div className="flex flex-wrap gap-2">
                            <strong>{signal.symbol}</strong>
                            <Direction side={signal.side} />
                          </div>
                          <p className="mt-1 text-xs text-muted-foreground">
                            <StrategyName id={signal.strategy_id} />
                          </p>
                          <time className="mt-1 block text-xs text-muted-foreground">
                            {time(signal.signal_ms)}
                          </time>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <EmptyState>
                      Signals will appear when a strategy's entry conditions are met.
                    </EmptyState>
                  )}
                </section>
                <section className="min-w-0">
                  <h3 className="mb-3 font-semibold">Open setups</h3>
                  {setups.length ? (
                    <ul className="divide-y divide-border">
                      {setups.map((setup) => (
                        <li key={setup.setup_id} className="py-3 text-sm">
                          <div className="flex flex-wrap gap-2">
                            <strong>{setup.symbol}</strong>
                            <Direction side={setup.side} />
                          </div>
                          <p className="mt-1 text-xs text-muted-foreground">
                            <StrategyName id={setup.strategy_id} />
                          </p>
                          <p className="text-xs">
                            <Hint term="reward/risk" /> <span className="num">{setup.rr}</span> ·{" "}
                            {time(setup.entry_ms)}
                          </p>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <EmptyState>
                      Trade setups appear after a signal fires and stay here until they finish.
                    </EmptyState>
                  )}
                </section>
              </div>
              <div className="grid min-w-0 gap-5 md:grid-cols-2">
                <section className="min-w-0">
                  <h3 className="mb-3 font-semibold">Paper wallet · realised balance</h3>
                  <EquityCurve points={equity} />
                </section>
                <section className="min-w-0">
                  <h3 className="mb-3 font-semibold">Open paper positions</h3>
                  {positions.length ? (
                    <ul className="space-y-2 text-sm">
                      {positions.map(([id, position]) => (
                        <li
                          key={id}
                          className="flex flex-wrap items-center gap-2 rounded-lg bg-secondary/30 p-3"
                        >
                          <strong>{String(position["symbol"])}</strong>
                          <Direction side={position["side"]} />
                          <span className="num">{String(position["leverage"])}× leverage</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <EmptyState>
                      No paper positions are open. Positions appear when the simulated wallet
                      accepts a setup.
                    </EmptyState>
                  )}
                </section>
              </div>
            </>
          )}
        </section>
        <TrendSection />
      </div>
    </AppShell>
  );
}

function TrendSection() {
  const queryClient = useQueryClient();
  const load = useServerFn(getTrendDashboard);
  const run = useServerFn(runForwardTrendNow);
  const [sample, setSample] = useState<"prospective" | "retrospective">("prospective");
  const data = useQuery({ queryKey: ["forward-trend-dashboard"], queryFn: () => load() });
  const runNow = useMutation({
    mutationFn: () => run(),
    onSuccess: (summary) => {
      toast(humanizeReason(summary.reason || summary.status).short);
      void queryClient.invalidateQueries({ queryKey: ["forward-trend-dashboard"] });
    },
    onError: (error) =>
      toast.error(
        humanizeReason(error instanceof Error ? error.message : "Portfolio update failed").short,
      ),
  });
  const tracks =
    (sample === "prospective" ? data.data?.tracks : data.data?.reconstructedTracks) ?? [];
  const liveTracks = data.data?.tracks ?? [];
  const byName = new Map(tracks.map((track) => [track.track, track]));
  const variants = tracks.filter((track) => track.kind === "variant");
  const equalWeight = byName.get("ew_long") ?? null;
  const latest = data.data?.latestAttempt ?? data.data?.latestRun;
  const status = explainTrendTrack(
    latest,
    Math.max(0, ...liveTracks.map((track) => track.prospectiveDays)),
    liveTracks.some((track) => track.kind === "variant" && track.currentWeights),
  );
  return (
    <section className="panel min-w-0 space-y-5 p-4 sm:p-6" aria-labelledby="trend-title">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="trend-title" className="flex items-center gap-2 text-xl font-semibold">
            <Layers3 className="size-5 text-primary" aria-hidden />
            Daily Trend Portfolios
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Compare daily portfolios with holding the same coins, after fees and funding.
          </p>
        </div>
        <Button onClick={() => runNow.mutate()} disabled={runNow.isPending}>
          {runNow.isPending ? "Updating portfolios…" : "Update trend portfolios now"}
        </Button>
      </div>
      {runNow.error && <LoadError error={runNow.error} />}
      {data.isPending ? (
        <p role="status" className="text-sm text-muted-foreground">
          Loading daily portfolios…
        </p>
      ) : data.error ? (
        <LoadError error={data.error} />
      ) : (
        <>
          <StatusSummary
            line={status}
            raw={
              latest
                ? `${latest.status} · ${latest.reason ?? "no reason reported"}`
                : "No run recorded"
            }
            updated={latest?.created_at ? new Date(latest.created_at).toLocaleString() : "—"}
          />
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex flex-wrap items-center gap-2 text-sm">
              Sample
              <select
                aria-label="Trend sample"
                className="max-w-full rounded-md border bg-background px-3 py-2 text-sm"
                value={sample}
                onChange={(event) => setSample(event.target.value as typeof sample)}
              >
                <option value="prospective">Live days</option>
                <option value="retrospective">Back-filled days</option>
              </select>
            </label>
            <p className="text-xs text-muted-foreground">
              {sample === "prospective"
                ? "Live days are scored after they happened."
                : "Back-filled days are replayed from history and are less trustworthy."}
            </p>
          </div>
          <details className="rounded-lg border border-warn/30 px-3 text-xs text-muted-foreground">
            <summary className="text-warn">How this simulation works</summary>
            <p>
              Hypothetical portfolios target volatility, with a 0.11% cost per unit of turnover plus
              funding payments. They have no margin, liquidation or position-size limits.
            </p>
            <p className="my-2">
              Live decisions were recorded before the daily close, sometimes after the open. Returns
              assume the full open-to-close day, so they are not a record of executable trades.
            </p>
            <p className="mb-2">
              Last scored daily close: {day(data.data?.latestRun?.through_day_ms)} (UTC). No variant
              has a validated edge.
            </p>
          </details>
          {variants.length === 0 ? (
            <EmptyState>
              No portfolios recorded yet. Update the portfolios to record weights; scored results
              appear after a daily close and a successful update.
            </EmptyState>
          ) : (
            <div className="grid min-w-0 gap-4 lg:grid-cols-2">
              {variants.map((track) => {
                const control = track.control ? byName.get(track.control) : undefined;
                return (
                  <article
                    key={track.track}
                    className="min-w-0 space-y-3 rounded-xl border bg-background/30 p-4"
                  >
                    <h3 className="font-semibold">
                      <StrategyName id={track.track} />
                    </h3>
                    <p className="text-xs text-muted-foreground">
                      <Hint term="prospective" />:{" "}
                      <span className="num">{track.prospectiveDays}</span> ·{" "}
                      <Hint term="reconstructed" />:{" "}
                      <span className="num">{track.retrospectiveDays}</span>
                    </p>
                    <dl className="grid grid-cols-2 gap-3 text-xs">
                      <div className="rounded-lg bg-secondary/40 p-3">
                        <dt className="text-muted-foreground">Average result / day</dt>
                        <dd className="num mt-1 text-lg">{pct(track.meanDaily)}</dd>
                      </div>
                      <div className="rounded-lg bg-secondary/40 p-3">
                        <dt className="text-muted-foreground">
                          <Hint term="vol-target">Buy and hold / day</Hint>
                        </dt>
                        <dd className="num mt-1 text-lg">{pct(control?.meanDaily ?? null)}</dd>
                      </div>
                      <div>
                        <dt
                          className="text-muted-foreground"
                          title="Long-only comparison with equal weight in each active coin."
                        >
                          Equal-weight hold / day
                        </dt>
                        <dd className="num">{pct(equalWeight?.meanDaily ?? null)}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">
                          <Hint term="turnover" />
                        </dt>
                        <dd className="num">
                          {track.meanTurnover === null
                            ? "—"
                            : `${(track.meanTurnover * 100).toFixed(2)}%`}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">
                          <Hint term="gross" />
                        </dt>
                        <dd className="num">
                          {track.meanGross === null ? "—" : `${track.meanGross.toFixed(2)}×`}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">
                          <Hint term="minimum detectable edge" />
                        </dt>
                        <dd className="num">
                          {track.muMinDaily === null
                            ? "More days needed"
                            : `${pct(track.muMinDaily)} / day`}
                        </dd>
                      </div>
                    </dl>
                    <section>
                      <h4 className="text-xs font-medium">
                        Current weights · {day(track.currentWeights?.day_ms)} (UTC)
                      </h4>
                      {track.currentWeights ? (
                        <>
                          <div className="mt-2 space-y-2">
                            {Object.entries(track.currentWeights.weights).map(
                              ([symbol, weight]) => (
                                <div
                                  key={symbol}
                                  className="grid grid-cols-[3rem_1fr_5rem] items-center gap-2 text-xs"
                                >
                                  <span>{symbol.replace("USDT", "")}</span>
                                  <div
                                    className="relative h-1.5 rounded-full bg-secondary"
                                    aria-hidden
                                  >
                                    <span className="absolute left-1/2 h-1.5 w-px bg-muted-foreground" />
                                    <span
                                      className={`absolute h-1.5 rounded-full ${weight < 0 ? "bg-bear" : "bg-bull"}`}
                                      style={{
                                        left: `${weight < 0 ? 50 - Math.min(Math.abs(weight), 2) * 25 : 50}%`,
                                        width: `${Math.min(Math.abs(weight), 2) * 25}%`,
                                      }}
                                    />
                                  </div>
                                  <span className="num text-right">
                                    {weight > 0 ? "+" : ""}
                                    {(weight * 100).toFixed(1)}%
                                  </span>
                                </div>
                              ),
                            )}
                          </div>
                          <p className="mt-2 text-xs text-muted-foreground">
                            Positive weights are long; negative weights are short.
                          </p>
                        </>
                      ) : (
                        <p className="mt-2 text-xs text-muted-foreground">
                          Weights appear after the first successful portfolio update.
                        </p>
                      )}
                    </section>
                    <MultiCurve
                      series={[track.equity, control?.equity ?? [], equalWeight?.equity ?? []]}
                    />
                    <details className="text-xs text-muted-foreground">
                      <summary>Technical details</summary>
                      <p>{strategyLabel(track.track).blurb}</p>
                      <p className="mt-2">{track.edgeLine}</p>
                      {track.currentWeights && (
                        <p className="mt-2">
                          Decided {time(track.currentWeights.decided_at_ms)}; recorded{" "}
                          {time(track.currentWeights.recorded_at_ms)} ·{" "}
                          {track.currentWeights.sample_kind}
                        </p>
                      )}
                      <p className="mt-2 break-words">
                        Strategy: {track.track} · control: {track.control ?? "none"} · equal weight:
                        ew_long · sample: {sample}
                      </p>
                      <p className="mt-2">
                        Comparison:{" "}
                        {track.control
                          ? strategyLabel(track.control).name
                          : "No matched comparison"}
                        . Turnover and gross exposure are averages over the selected sample.
                      </p>
                    </details>
                  </article>
                );
              })}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function MultiCurve({ series }: { series: { day_ms: number; equity: number }[][] }) {
  const points = series.flat();
  if (!series[0] || series[0].length < 2)
    return (
      <p className="rounded-lg bg-secondary/20 p-3 text-xs text-muted-foreground">
        The growth chart appears after two scored days in this sample.
      </p>
    );
  const width = 300,
    height = 100;
  const xs = points.map((p) => p.day_ms),
    ys = points.map((p) => p.equity);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const path = (line: { day_ms: number; equity: number }[]) =>
    line
      .map(
        (p, i) =>
          `${i ? "L" : "M"}${((p.day_ms - x0!) / (x1! - x0! || 1)) * width},${height - ((p.equity - y0!) / (y1! - y0! || 1)) * height}`,
      )
      .join(" ");
  const styles = ["text-primary", "text-muted-foreground", "text-warn"];
  return (
    <figure className="space-y-2 border-t pt-3">
      <figcaption className="text-xs text-muted-foreground">
        Hypothetical growth · 1× starting value
      </figcaption>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-28 w-full"
        role="img"
        aria-label={`Portfolio growth from ${day(x0)} to ${day(x1)}; range ${y0!.toFixed(3)} to ${y1!.toFixed(3)} times starting value`}
      >
        {series.map((line, index) =>
          line.length > 1 ? (
            <path
              key={index}
              d={path(line)}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeDasharray={index === 1 ? "6 3" : index === 2 ? "2 3" : undefined}
              className={styles[index]}
            />
          ) : null,
        )}
      </svg>
      <div className="flex justify-between text-[11px] text-muted-foreground">
        <span>{day(x0)}</span>
        <span>{day(x1)} · UTC</span>
      </div>
      <ul className="space-y-1 text-xs">
        {[
          "Strategy (solid)",
          "Volatility-targeted hold (dashed)",
          "Equal-weight hold (dotted)",
        ].map((label, i) => (
          <li key={label} className={styles[i]}>
            {label}:{" "}
            <span className="num">
              {series[i]?.length ? `${series[i]!.at(-1)!.equity.toFixed(3)}×` : "not yet available"}
            </span>
          </li>
        ))}
      </ul>
    </figure>
  );
}

function EquityCurve({ points }: { points: { ms: number; balance: number }[] }) {
  if (points.length < 2)
    return (
      <EmptyState>The balance chart appears after the paper wallet records two entries.</EmptyState>
    );
  const width = 480,
    height = 160;
  const xs = points.map((p) => p.ms),
    ys = points.map((p) => p.balance);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const path = points
    .map(
      (p, i) =>
        `${i ? "L" : "M"}${((p.ms - x0!) / (x1! - x0! || 1)) * width},${height - ((p.balance - y0!) / (y1! - y0! || 1)) * height}`,
    )
    .join(" ");
  return (
    <figure>
      <figcaption className="num mb-3 text-lg">
        {points.at(-1)!.balance.toLocaleString(undefined, { maximumFractionDigits: 2 })} USDT
      </figcaption>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-40 w-full text-primary"
        role="img"
        aria-label={`Realised paper wallet balance; range ${y0!.toFixed(2)} to ${y1!.toFixed(2)} USDT`}
      >
        <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" />
      </svg>
      <div className="mt-2 flex justify-between gap-2 text-[11px] text-muted-foreground">
        <span>{time(x0)}</span>
        <span>{time(x1)}</span>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        Realised balance excludes unrealised gains or losses on open positions.
      </p>
    </figure>
  );
}
