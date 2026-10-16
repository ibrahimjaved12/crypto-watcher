import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useMemo, useRef, useState } from "react";
import { ArrowDownRight, ArrowUpRight, BellRing, ChevronRight, Download, FlaskConical, Trash2 } from "lucide-react";
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
import { Hint } from "@/components/hint";
import { EmptyState, PageHeader, TimeAgo } from "@/components/plain";
import { relativeTime, sourceLabel } from "@/lib/labels";
import { cn } from "@/lib/utils";
import { createTestAlert, fetchAlertCharts, fetchAlerts, fetchWatchlist, type AlertRow } from "@/lib/db";
import { AlertChartPanel } from "@/components/alert-chart-panel";
import { chartViewModel, type ChartMeta } from "@/lib/alert-charts/hosted";
import { deleteAlertWithChart } from "@/lib/alert-charts/view.functions";
import { WINDOW_LABELS } from "@/lib/market/symbols";

export const Route = createFileRoute("/_authenticated/alerts")({
  head: () => ({
    meta: [
      { title: "Alerts — Crypto Watch" },
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

  // Rows open independently (several at once). Charts load lazily, only for open rows (see AlertChartPanel).
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;
  const alerts = useQuery({ queryKey: ["alerts"], queryFn: fetchAlerts });
  const alertsRef = useRef(alerts.data);
  alertsRef.current = alerts.data;
  // Chart metadata is small; it refreshes every 60 s (the existing automatic cadence) only while an open row
  // shows a state that can still change (waiting, drawing, uploading).
  const charts = useQuery({
    queryKey: ["alert-charts"],
    queryFn: fetchAlertCharts,
    refetchInterval: (query) => {
      const byId = new Map((query.state.data ?? []).map((row) => [row.alert_id, row as ChartMeta]));
      const rows = alertsRef.current ?? [];
      return rows.some((a) => expandedRef.current.has(a.id) && chartViewModel(a, byId.get(a.id), Date.now()).changing)
        ? 60_000
        : false;
    },
  });
  const chartById = useMemo(
    () => new Map((charts.data ?? []).map((row) => [row.alert_id, row as ChartMeta])),
    [charts.data],
  );
  const toggle = (id: string) =>
    setExpanded((open) => {
      const next = new Set(open);
      if (!next.delete(id)) next.add(id);
      return next;
    });
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
    onError: (e: Error) => toast.error(e.message),
  });

  // Also removes the hosted chart (stored image and its metadata row) in the same server call.
  const remove = useMutation({
    mutationFn: (alertId: string) => deleteAlertWithChart({ data: { alertId } }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["alert-charts"] });
    },
    onError: (e: Error) => toast.error(e.message),
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
        eyebrow="Mission log"
        title="Alerts"
        subtitle={
          <>
            Every price move that crossed your alert threshold, with the rule and data behind it.{" "}
            <span className="num">
              {filtered.length} of {alerts.data?.length ?? 0}
            </span>{" "}
            shown.
          </>
        }
        actions={
          <>
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search pair, rule, source…"
              className="h-9 w-full sm:w-56"
              aria-label="Search alerts"
            />
            <Button variant="secondary" onClick={exportCsv} disabled={filtered.length === 0}>
              <Download className="size-4" aria-hidden />
              Export CSV
            </Button>
            <Button
              variant="secondary"
              onClick={() => test.mutate()}
              title="Save a fake alert, clearly marked as test data, to check that alerts are stored"
            >
              <FlaskConical className="size-4" aria-hidden />
              Save a test alert
            </Button>
          </>
        }
      />

      <div className="panel mt-5 overflow-x-auto">
        {filtered.length === 0 ? (
          <EmptyState
            icon={BellRing}
            className="m-4 border-0"
            title={search ? "No alerts match your search" : "No alerts yet"}
            body={
              search
                ? "Try a pair such as BTC, or clear the search."
                : "An alert appears here when a watched pair moves more than your threshold from its starting price. Your machine checks about once a minute."
            }
          />
        ) : (
          <Table className="min-w-[720px]">
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">
                  <span className="sr-only">Chart</span>
                </TableHead>
                <TableHead>Pair</TableHead>
                <TableHead>When</TableHead>
                <TableHead>Move</TableHead>
                <TableHead>
                  <Hint text="The price the move was measured from: your saved starting price, or a fixed time window for older alerts.">
                    Measured from
                  </Hint>
                </TableHead>
                <TableHead>Rule</TableHead>
                <TableHead className="text-right">Price</TableHead>
                <TableHead>Source</TableHead>
                <TableHead>
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((a) => {
                const change = Number(a.change_pct);
                const up = change >= 0;
                const Arrow = up ? ArrowUpRight : ArrowDownRight;
                const open = expanded.has(a.id);
                const meta = chartById.get(a.id);
                const chart = chartViewModel(a, meta, Date.now());
                const panelId = `alert-chart-${a.id}`;
                return (
                  <Fragment key={a.id}>
                  <TableRow>
                    <TableCell className="w-10 px-2">
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-expanded={open}
                        aria-controls={panelId}
                        aria-label={`${open ? "Hide" : "Show"} setup chart for ${a.symbol}${chart.ready ? " (ready)" : ""}`}
                        title="Setup chart"
                        onClick={() => toggle(a.id)}
                        className="relative"
                      >
                        <ChevronRight className={cn("size-4 transition-transform", open && "rotate-90")} aria-hidden />
                        {chart.ready ? (
                          <span className="absolute right-1 top-1 size-2 rounded-full bg-bull" aria-hidden />
                        ) : null}
                      </Button>
                    </TableCell>
                    <TableCell className="num font-medium">
                      {a.symbol.replace(/USDT$/, "")}
                      <span className="text-muted-foreground">/USDT</span>
                      {a.is_test ? (
                        <Badge variant="outline" className="ml-2 border-warn/60 text-warn">
                          Test data
                        </Badge>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-xs">
                      <TimeAgo at={a.triggered_at} text={relativeTime(a.triggered_at)} />
                    </TableCell>
                    <TableCell>
                      <span
                        className={cn(
                          "num inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium",
                          up ? "border-bull/40 bg-bull/10 text-bull" : "border-bear/40 bg-bear/10 text-bear",
                        )}
                      >
                        <Arrow className="size-3.5" aria-hidden />
                        {up ? "Up" : "Down"} {Math.abs(change).toFixed(2)}%
                      </span>
                    </TableCell>
                    <TableCell className="num text-xs">
                      {a.comparison_mode === "baseline" ? (
                        <span
                          title={
                            a.baseline_at
                              ? `Starting price saved ${new Date(a.baseline_at).toLocaleString()}`
                              : undefined
                          }
                        >
                          {Number(a.baseline_price).toLocaleString()} USDT
                        </span>
                      ) : a.window_minutes == null ? (
                        "—"
                      ) : (
                        `${WINDOW_LABELS[a.window_minutes] ?? `${a.window_minutes}m`} ago`
                      )}
                    </TableCell>
                    <TableCell className="max-w-[220px] truncate text-xs" title={a.rule}>
                      {a.rule}
                    </TableCell>
                    <TableCell className="num text-right text-xs">
                      {a.price == null ? "—" : `${Number(a.price).toLocaleString()} USDT`}
                    </TableCell>
                    <TableCell className="text-xs" title={a.data_source ?? undefined}>
                      {sourceLabel(a.data_source)}
                    </TableCell>
                    <TableCell>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label="Delete alert"
                        title="Delete alert"
                        onClick={() => remove.mutate(a.id)}
                      >
                        <Trash2 className="size-4" aria-hidden />
                      </Button>
                    </TableCell>
                  </TableRow>
                  {open ? (
                    <TableRow className="hover:bg-transparent">
                      <TableCell colSpan={9} className="whitespace-normal bg-muted/20 p-0">
                        <AlertChartPanel alert={a} meta={meta} id={panelId} />
                      </TableCell>
                    </TableRow>
                  ) : null}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
        )}
      </div>
    </AppShell>
  );
}
