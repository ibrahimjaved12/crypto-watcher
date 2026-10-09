import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { summarizeOutcomes } from "@/lib/forward/forward-dashboard";
import { getForwardDashboard, getTrendDashboard, runForwardNow, runForwardTrendNow } from "@/lib/forward.functions";

export const Route = createFileRoute("/_authenticated/forward")({
  head: () => ({ meta: [{ title: "Forward test — Crypto Watch" }] }),
  component: ForwardPage,
});

const time = (ms: number | null | undefined) => (ms ? new Date(ms).toISOString().slice(0, 16).replace("T", " ") : "—");
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
      toast(`Forward run ${summary.status}${summary.reason ? `: ${summary.reason}` : ""}`);
      void queryClient.invalidateQueries({ queryKey: ["forward-dashboard"] });
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Forward run failed"),
  });
  const summary = useMemo(
    () => summarizeOutcomes(data.data?.outcomeRows ?? [], Date.now() - sinceDays * 86_400_000),
    [data.data, sinceDays],
  );
  const latest = data.data?.latestRun as
    | { status: string; reason: string | null; boundary_ms: number; wallet_state: { positions?: Record<string, Record<string, unknown>> } | null }
    | null
    | undefined;
  const fresh = latest?.status === "ok" && Date.now() - latest.boundary_ms < 2 * 3_600_000;
  const equity = data.data?.equity ?? [];
  const positions = Object.entries(latest?.wallet_state?.positions ?? {});

  return (
    <AppShell>
      <div className="space-y-6 p-4">
        <div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
          No strategy has a validated edge; this is a forward test.
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold">Forward test</h1>
          <Badge variant={fresh ? "default" : "destructive"}>
            {latest ? `${fresh ? "fresh" : "stale"} · last run ${time(latest.boundary_ms)} · ${latest.status}` : "no runs yet"}
          </Badge>
          {latest?.reason ? <span className="text-xs text-muted-foreground">{latest.reason}</span> : null}
          <Button size="sm" onClick={() => runNow.mutate()} disabled={runNow.isPending}>Run now</Button>
        </div>

        <section>
          <div className="mb-2 flex items-center gap-2">
            <h2 className="font-medium">Outcomes by strategy (final, micro-R → R)</h2>
            <select className="rounded border bg-background px-2 py-1 text-sm" value={sinceDays}
              onChange={(event) => setSinceDays(Number(event.target.value))}>
              {[7, 30, 90, 365].map((days) => <option key={days} value={days}>last {days} days</option>)}
            </select>
          </div>
          <table className="w-full text-sm">
            <thead><tr className="text-left text-muted-foreground">
              <th>n</th><th>strategy</th><th>version</th><th>rr</th><th>wins</th><th>ambiguous</th>
              <th>mean net R</th><th>placebo n</th><th>placebo mean net R</th><th>score band</th>
            </tr></thead>
            <tbody>
              {summary.map((row) => (
                <tr key={`${row.strategyId}|${row.rr}`} className="border-t">
                  <td>{row.n}</td><td>{row.strategyId}</td><td>{row.version}</td><td>{row.rr}</td>
                  <td>{row.wins}</td><td>{row.ambiguous}</td><td>{r(row.meanNetR)}</td>
                  <td>{row.placebo?.n ?? 0}</td><td>{r(row.placebo?.meanNetR ?? null)}</td>
                  <td>n/a (no score yet)</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="grid gap-6 md:grid-cols-2">
          <div>
            <h2 className="mb-2 font-medium">Latest signals</h2>
            <ul className="space-y-1 text-sm">
              {(data.data?.signals ?? []).slice(0, 20).map((signal) => (
                <li key={signal.signal_id}>
                  {time(signal.signal_ms)} · {signal.symbol} · {signal.side === 1 ? "long" : "short"} · {signal.strategy_id}
                </li>
              ))}
            </ul>
          </div>
          <div>
            <h2 className="mb-2 font-medium">Open setups</h2>
            <ul className="space-y-1 text-sm">
              {(data.data?.openSetups ?? []).slice(0, 20).map((setup) => (
                <li key={setup.setup_id}>
                  {time(setup.entry_ms)} · {setup.symbol} · {setup.side === 1 ? "long" : "short"} · {setup.strategy_id} · rr {setup.rr}
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section className="grid gap-6 md:grid-cols-2">
          <div>
            <h2 className="mb-2 font-medium">Paper wallet equity (USDT, realized)</h2>
            <EquityCurve points={equity} />
          </div>
          <div>
            <h2 className="mb-2 font-medium">Open paper positions</h2>
            <ul className="space-y-1 text-sm">
              {positions.map(([id, position]) => (
                <li key={id}>
                  {String(position["symbol"])} · {position["side"] === 1 ? "long" : "short"} · leverage {String(position["leverage"])}x
                </li>
              ))}
              {positions.length === 0 ? <li className="text-muted-foreground">none</li> : null}
            </ul>
          </div>
        </section>

        <TrendSection />
      </div>
    </AppShell>
  );
}

const pct = (value: number | null, digits = 3) => (value === null ? "—" : `${(value * 100).toFixed(digits)}%`);
const day = (ms: number | null | undefined) => (ms === null || ms === undefined ? "—" : new Date(ms).toISOString().slice(0, 10));

function TrendSection() {
  const queryClient = useQueryClient();
  const load = useServerFn(getTrendDashboard);
  const run = useServerFn(runForwardTrendNow);
  const data = useQuery({ queryKey: ["forward-trend-dashboard"], queryFn: () => load() });
  const runNow = useMutation({
    mutationFn: () => run(),
    onSuccess: (summary) => {
      toast(`Trend track run ${summary.status}${summary.reason ? `: ${summary.reason}` : ""}`);
      void queryClient.invalidateQueries({ queryKey: ["forward-trend-dashboard"] });
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Trend track run failed"),
  });
  const tracks = data.data?.tracks ?? [];
  const byName = new Map(tracks.map((track) => [track.track, track]));
  const variants = tracks.filter((track) => track.kind === "variant");
  const equalWeight = byName.get("ew_long") ?? null;
  const latest = data.data?.latestRun ?? null;
  return (
    <section className="space-y-3">
      <div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
        Daily trend (Mode B) forward track: a hypothetical vol-targeted portfolio per variant, with 11 bp per unit
        turnover and funding but no margin, liquidation or position limits. No variant has a validated edge.
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="font-medium">Daily trend track (portfolio mode)</h2>
        <Badge variant={latest?.status === "ok" ? "default" : "destructive"}>
          {latest ? `${latest.status} · through ${day(latest.through_day_ms)}` : "no runs yet"}
        </Badge>
        {latest?.reason ? <span className="text-xs text-muted-foreground">{latest.reason}</span> : null}
        <Button size="sm" onClick={() => runNow.mutate()} disabled={runNow.isPending}>Run trend now</Button>
      </div>
      <table className="w-full text-sm">
        <thead><tr className="text-left text-muted-foreground">
          <th>variant</th><th>days</th><th>mean/day</th><th>vol-target B&amp;H mean/day</th><th>equal-weight mean/day</th>
          <th>turnover/day</th><th>gross</th><th>current weights</th><th>significance</th>
        </tr></thead>
        <tbody>
          {variants.map((track) => {
            const control = track.control ? byName.get(track.control) : undefined;
            return (
              <tr key={track.track} className="border-t align-top">
                <td>{track.track}</td><td>{track.days}</td><td>{pct(track.meanDaily)}</td>
                <td>{pct(control?.meanDaily ?? null)}</td><td>{pct(equalWeight?.meanDaily ?? null)}</td>
                <td>{track.meanTurnover === null ? "—" : track.meanTurnover.toFixed(3)}</td>
                <td>{track.meanGross === null ? "—" : track.meanGross.toFixed(2)}</td>
                <td className="text-xs">
                  {track.currentWeights
                    ? `${day(track.currentWeights.day_ms)}: ` + Object.entries(track.currentWeights.weights)
                      .map(([symbol, weight]) => `${symbol.replace("USDT", "")} ${weight.toFixed(2)}`).join(", ")
                    : "—"}
                </td>
                <td className="text-xs">{track.significanceLine}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="grid gap-4 md:grid-cols-3">
        {variants.map((track) => (
          <div key={track.track}>
            <h3 className="text-sm">{track.track} vs {track.control ?? "control"} and ew_long (equity)</h3>
            <MultiCurve series={[track.equity, (track.control ? byName.get(track.control)?.equity : undefined) ?? [],
              equalWeight?.equity ?? []]} />
          </div>
        ))}
      </div>
    </section>
  );
}

function MultiCurve({ series }: { series: { day_ms: number; equity: number }[][] }) {
  const points = series.flat();
  if (series[0]!.length < 2) return <p className="text-sm text-muted-foreground">Not enough days yet.</p>;
  const width = 300;
  const height = 100;
  const xs = points.map((p) => p.day_ms);
  const ys = points.map((p) => p.equity);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const path = (line: { day_ms: number; equity: number }[]) => line
    .map((p, i) => `${i ? "L" : "M"}${((p.day_ms - x0) / (x1 - x0 || 1)) * width},${height - ((p.equity - y0) / (y1 - y0 || 1)) * height}`)
    .join(" ");
  const styles = ["text-primary", "text-muted-foreground", "text-amber-500"];
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-24 w-full" role="img" aria-label="Trend track equity">
      {series.map((line, index) => line.length > 1
        ? <path key={index} d={path(line)} fill="none" stroke="currentColor" strokeWidth="1.2" className={styles[index]} />
        : null)}
    </svg>
  );
}

function EquityCurve({ points }: { points: { ms: number; balance: number }[] }) {
  if (points.length < 2) return <p className="text-sm text-muted-foreground">No ledger entries yet.</p>;
  const width = 480;
  const height = 160;
  const xs = points.map((p) => p.ms);
  const ys = points.map((p) => p.balance);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const path = points
    .map((p, i) => `${i ? "L" : "M"}${((p.ms - x0) / (x1 - x0 || 1)) * width},${height - ((p.balance - y0) / (y1 - y0 || 1)) * height}`)
    .join(" ");
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-40 w-full text-primary" role="img" aria-label="Paper wallet equity">
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}
