import { PageHeader, QueryNotice, TechnicalDetails } from "@/components/presentation";
import { alertRuleLabel, sourceLabel, pairLabel, issueSummary } from "@/lib/presentation/labels";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Download, FlaskConical, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { createTestAlert, deleteAlert, fetchAlerts, fetchWatchlist, type AlertRow } from "@/lib/db";
import { WINDOW_LABELS } from "@/lib/market/symbols";

export const Route = createFileRoute("/_authenticated/alerts")({
  head: () => ({
    meta: [
      { title: "Alert history — Crypto Watch" },
      {
        name: "description",
        content: "Search your saved price alerts, see the rule behind each one, and export to CSV.",
      },
      { property: "og:title", content: "Alert history — Crypto Watch" },
      { property: "og:description", content: "Searchable alert history with CSV export." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: AlertsPage,
});

function toCsv(rows: AlertRow[]): string {
  const header = [
    "symbol",
    "triggered_at",
    "change_pct",
    "window_minutes",
    "threshold_pct",
    "rule",
    "price",
    "data_source",
    "is_test",
    "comparison_mode",
    "baseline_price",
    "baseline_at",
    "observed_at",
  ];
  const escape = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  return [
    header.join(","),
    ...rows.map((r) => header.map((k) => escape(r[k as keyof AlertRow])).join(",")),
  ].join("\n");
}

function AlertsPage() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");

  const alerts = useQuery({ queryKey: ["alerts"], queryFn: fetchAlerts });
  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: fetchWatchlist });

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const rows = alerts.data ?? [];
    if (!q) return rows;
    return rows.filter((r) =>
      [
        r.symbol,
        pairLabel(r.symbol),
        r.rule,
        alertRuleLabel(r),
        r.data_source,
        sourceLabel(r.data_source),
        new Date(r.triggered_at).toLocaleString(),
      ]
        .join(" ")
        .toLowerCase()
        .includes(q),
    );
  }, [alerts.data, search]);

  const test = useMutation({
    mutationFn: () => createTestAlert(watchlist.data?.[0]?.symbol ?? "BTCUSDT"),
    onSuccess: () => {
      toast.success("Test alert saved — it is marked as test data.");
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
    },
    onError: (e: Error) => toast.error(issueSummary(e.message)),
  });

  const remove = useMutation({
    mutationFn: deleteAlert,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["alerts"] }),
  });

  function exportCsv() {
    const blob = new Blob([toCsv(filtered)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `crypto-watch-alerts-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <AppShell>
      <PageHeader
        title="Alerts"
        description={`Saved market moves and the rules behind them. ${filtered.length} of ${alerts.data?.length ?? 0} alerts.`}
      >
        <div className="ml-auto flex flex-wrap gap-2">
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search symbol, rule, source…"
            className="w-56"
            aria-label="Search alerts"
          />
          <Button variant="secondary" onClick={exportCsv} disabled={filtered.length === 0}>
            <Download className="size-4" aria-hidden />
            Export CSV
          </Button>
          <Button variant="secondary" onClick={() => test.mutate()} disabled={test.isPending}>
            <FlaskConical className="size-4" aria-hidden />
            Test alert
          </Button>
        </div>
      </PageHeader>
      <QueryNotice pending={alerts.isPending} error={alerts.error} />
      {(test.error || remove.error) && (
        <div role="alert" className="text-sm text-destructive">
          {issueSummary((test.error || remove.error)!.message)}
          <TechnicalDetails>{(test.error || remove.error)!.message}</TechnicalDetails>
        </div>
      )}

      <div className="panel mt-5 overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Symbol</TableHead>
              <TableHead>Time</TableHead>
              <TableHead>Change</TableHead>
              <TableHead>Comparison</TableHead>
              <TableHead>Rule</TableHead>
              <TableHead>Price</TableHead>
              <TableHead>Source</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((a) => (
              <TableRow key={a.id}>
                <TableCell className="num font-medium">
                  {pairLabel(a.symbol)}
                  {a.is_test ? (
                    <Badge variant="outline" className="ml-2 border-warn/60 text-warn">
                      TEST DATA
                    </Badge>
                  ) : null}
                </TableCell>
                <TableCell className="num text-xs">
                  {new Date(a.triggered_at).toLocaleString()}
                </TableCell>
                <TableCell
                  className={`num ${Number(a.change_pct) >= 0 ? "text-bull" : "text-bear"}`}
                >
                  {Number(a.change_pct) > 0 ? "+" : ""}
                  {Number(a.change_pct).toFixed(2)}%
                </TableCell>
                <TableCell className="num text-xs">
                  {a.comparison_mode === "baseline" ? (
                    <span
                      title={
                        a.baseline_at
                          ? `Baseline at ${new Date(a.baseline_at).toLocaleString()}`
                          : undefined
                      }
                    >
                      From{" "}
                      {a.baseline_price === null
                        ? "unavailable baseline"
                        : `${Number(a.baseline_price).toLocaleString()} USDT`}
                    </span>
                  ) : a.window_minutes == null ? (
                    "—"
                  ) : (
                    (WINDOW_LABELS[a.window_minutes] ?? `${a.window_minutes}m`)
                  )}
                </TableCell>
                <TableCell className="min-w-48 max-w-[280px] text-xs">
                  {alertRuleLabel(a)}
                  <TechnicalDetails>{a.rule}</TechnicalDetails>
                </TableCell>
                <TableCell className="num text-xs">
                  {a.price == null ? "—" : `$${Number(a.price).toLocaleString()}`}
                </TableCell>
                <TableCell className="text-xs">{sourceLabel(a.data_source)}</TableCell>
                <TableCell>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Delete alert"
                    onClick={() => remove.mutate(a.id)}
                  >
                    <Trash2 className="size-4" aria-hidden />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
            {!alerts.isPending && !alerts.error && filtered.length === 0 ? (
              <TableRow>
                <TableCell colSpan={8} className="py-10 text-center text-sm text-muted-foreground">
                  {search
                    ? "No alerts match your search. Try a pair or data source."
                    : "No saved alerts yet. A qualifying move from your saved baseline will appear here."}
                </TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>
      </div>
    </AppShell>
  );
}
