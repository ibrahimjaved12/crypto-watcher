import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ArrowDownRight, ArrowUpRight, Bell, Download, FlaskConical, Trash2 } from "lucide-react";
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
import { humanizeReason } from "@/lib/labels";
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

function relativeTime(value: string, now: number) {
  const elapsed = (Date.parse(value) - now) / 1_000;
  if (!Number.isFinite(elapsed)) return "Time unknown";
  if (Math.abs(elapsed) < 60) return "Just now";
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "short" });
  if (Math.abs(elapsed) < 3_600) return formatter.format(Math.round(elapsed / 60), "minute");
  if (Math.abs(elapsed) < 86_400) return formatter.format(Math.round(elapsed / 3_600), "hour");
  return formatter.format(Math.round(elapsed / 86_400), "day");
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
      [r.symbol, r.rule, r.data_source, new Date(r.triggered_at).toLocaleString()]
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
    onError: (e: Error) => toast.error(humanizeReason(e.message).short),
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
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Alert history</h1>
          <p className="text-sm text-muted-foreground">
            Price moves that crossed your alert rules, saved for review.
          </p>
        </div>
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
          <Button
            variant="secondary"
            onClick={() => test.mutate()}
            disabled={test.isPending}
            title="Save a sample alert, clearly marked as test data"
          >
            <FlaskConical className="size-4" aria-hidden />
            Test alert
          </Button>
        </div>
      </div>

      <p className="mt-5 text-xs text-muted-foreground">
        {filtered.length} of {alerts.data?.length ?? 0} alerts
      </p>
      {alerts.error && (
        <div role="alert" className="mt-3 text-sm text-bear">
          {humanizeReason(alerts.error.message).short}
          <details className="text-xs">
            <summary>Details</summary>
            {alerts.error.message}
          </details>
        </div>
      )}
      <div
        className="panel mt-3 min-w-0 overflow-x-auto"
        tabIndex={0}
        role="region"
        aria-label="Saved alerts"
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Pair</TableHead>
              <TableHead>When</TableHead>
              <TableHead>Price move</TableHead>
              <TableHead>Compared with</TableHead>
              <TableHead>Why it fired</TableHead>
              <TableHead>Price (USDT)</TableHead>
              <TableHead>Source</TableHead>
              <TableHead>
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((a) => (
              <TableRow key={a.id}>
                <TableCell className="num font-medium">
                  {a.symbol}
                  {a.is_test ? (
                    <Badge variant="outline" className="ml-2 border-warn/60 text-warn">
                      Test data
                    </Badge>
                  ) : null}
                </TableCell>
                <TableCell className="num text-xs">
                  <time dateTime={a.triggered_at} title={new Date(a.triggered_at).toLocaleString()}>
                    {relativeTime(a.triggered_at, Date.now())}
                  </time>
                </TableCell>
                <TableCell
                  className={`num ${Number(a.change_pct) >= 0 ? "text-bull" : "text-bear"}`}
                >
                  <span className="inline-flex items-center gap-1 rounded-full border border-current/30 px-2 py-1 text-xs">
                    {Number(a.change_pct) >= 0 ? (
                      <ArrowUpRight className="size-3" aria-hidden />
                    ) : (
                      <ArrowDownRight className="size-3" aria-hidden />
                    )}
                    {Number(a.change_pct) >= 0 ? "Up" : "Down"}
                  </span>
                  <span className="mt-1 block">
                    {Number(a.change_pct) > 0 ? "+" : ""}
                    {Number(a.change_pct).toFixed(2)}%
                  </span>
                </TableCell>
                <TableCell className="num text-xs">
                  {a.comparison_mode === "baseline" ? (
                    <span
                      title={
                        a.baseline_at
                          ? `Reference price saved at ${new Date(a.baseline_at).toLocaleString()}`
                          : undefined
                      }
                    >
                      From {Number(a.baseline_price).toLocaleString()} USDT
                    </span>
                  ) : a.window_minutes == null ? (
                    "—"
                  ) : (
                    (WINDOW_LABELS[a.window_minutes] ?? `${a.window_minutes}m`)
                  )}
                </TableCell>
                <TableCell className="min-w-44 max-w-64 text-xs">
                  {a.is_test
                    ? "Manual test alert"
                    : `Moved ${Number(a.threshold_pct).toFixed(2)}% ${a.comparison_mode === "baseline" ? "from the saved reference" : "within the selected window"}`}
                  <details className="mt-1 break-words text-muted-foreground">
                    <summary>Details</summary>
                    {a.rule}
                    <p>
                      Comparison: {a.comparison_mode} · observed:{" "}
                      {a.observed_at ? new Date(a.observed_at).toLocaleString() : "unknown"}
                    </p>
                  </details>
                </TableCell>
                <TableCell className="num text-xs">
                  {a.price == null
                    ? "—"
                    : Number(a.price).toLocaleString(undefined, { maximumFractionDigits: 6 })}
                </TableCell>
                <TableCell className="text-xs">{humanizeReason(a.data_source).short}</TableCell>
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
            {filtered.length === 0 ? (
              <TableRow>
                <TableCell colSpan={8} className="py-10 text-center text-sm text-muted-foreground">
                  {alerts.isPending ? (
                    "Loading your alerts…"
                  ) : alerts.isError ? (
                    "Alerts could not be loaded. Refresh the page to try again."
                  ) : search ? (
                    "No alerts match this search. Try another pair or clear the filter."
                  ) : (
                    <span className="inline-flex max-w-md flex-col items-center gap-3">
                      <Bell className="size-7 text-primary" aria-hidden />
                      No alerts yet. They appear when a watched pair crosses your threshold; use
                      Test alert to see an example.
                    </span>
                  )}
                </TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>
      </div>
    </AppShell>
  );
}
