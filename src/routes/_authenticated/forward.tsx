import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { summarizeOutcomes } from "@/lib/forward/forward-dashboard";
import { getForwardDashboard, runForwardNow } from "@/lib/forward.functions";

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
      </div>
    </AppShell>
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
