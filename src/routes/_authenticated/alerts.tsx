import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Download, FlaskConical, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { EmptyState, ErrorNotice, PageHeader, TechnicalDetails } from "@/components/presentation";
import { alertRuleLabel, issueSummary, pairLabel, sourceLabel } from "@/lib/presentation/labels";
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
        description="Saved price movements from the pairs you follow. Review what changed and the rule that triggered it."
      >
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
        <Button variant="outline" onClick={() => test.mutate()} disabled={test.isPending}>
          <FlaskConical className="size-4" aria-hidden />
          Create test alert
        </Button>
      </PageHeader>
      <p className="mt-5 text-xs text-muted-foreground">
        {alerts.isPending
          ? "Loading alerts…"
          : `${filtered.length} of ${alerts.data?.length ?? 0} saved alerts`}
      </p>
      {alerts.error && <ErrorNotice title="Alert history unavailable." error={alerts.error} />}
      {remove.error && <ErrorNotice title="Alert could not be deleted." error={remove.error} />}
      {test.error && <ErrorNotice title="Test alert could not be saved." error={test.error} />}
      <div className="panel mt-3 overflow-x-auto">
        <Table className="data-table min-w-[1050px]" tabIndex={0} aria-label="Alert history">
          <caption className="sr-only">Saved movement alerts</caption>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Pair</TableHead>
              <TableHead scope="col">Time</TableHead>
              <TableHead scope="col">Change</TableHead>
              <TableHead scope="col">Comparison</TableHead>
              <TableHead scope="col">Rule</TableHead>
              <TableHead scope="col">Price</TableHead>
              <TableHead scope="col">Source</TableHead>
              <TableHead scope="col">
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((a) => (
              <TableRow key={a.id} className={a.is_test ? "bg-warn/5" : undefined}>
                <TableCell className="font-medium">
                  {pairLabel(a.symbol)}
                  {a.is_test ? (
                    <Badge
                      variant="outline"
                      className="ml-2 border-warn/60 bg-warn/10 font-bold text-warn"
                    >
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
                    <span>
                      From{" "}
                      {a.baseline_price == null
                        ? "unavailable baseline"
                        : `${Number(a.baseline_price).toLocaleString()} USDT`}
                    </span>
                  ) : a.window_minutes == null ? (
                    "—"
                  ) : (
                    (WINDOW_LABELS[a.window_minutes] ?? `${a.window_minutes}m`)
                  )}
                </TableCell>
                <TableCell className="min-w-52 max-w-72 text-xs">
                  {alertRuleLabel(a)}
                  <TechnicalDetails className="mt-2">
                    <p>
                      Saved rule: <code>{a.rule}</code>
                    </p>
                    <p>
                      Source: <code>{a.data_source}</code> · Comparison:{" "}
                      <code>{a.comparison_mode}</code>
                    </p>
                    <p>
                      Baseline time:{" "}
                      {a.baseline_at ? new Date(a.baseline_at).toLocaleString() : "—"}
                    </p>
                    <p>
                      Observed: {a.observed_at ? new Date(a.observed_at).toLocaleString() : "—"}
                    </p>
                  </TechnicalDetails>
                </TableCell>
                <TableCell className="num text-xs">
                  {a.price == null ? "—" : `$${Number(a.price).toLocaleString()}`}
                </TableCell>
                <TableCell className="text-xs">{sourceLabel(a.data_source)}</TableCell>
                <TableCell>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Delete ${a.is_test ? "test " : ""}alert for ${pairLabel(a.symbol)}`}
                    disabled={remove.isPending}
                    onClick={() => remove.mutate(a.id)}
                  >
                    <Trash2 className="size-4" aria-hidden />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
            {filtered.length === 0 && !alerts.isPending && !alerts.isError ? (
              <TableRow>
                <TableCell colSpan={8} className="py-10 text-center text-sm text-muted-foreground">
                  <EmptyState title={search ? "No matching alerts" : "No saved movements yet"}>
                    {search
                      ? "Try another pair, rule or source."
                      : "Qualifying price movements appear here after a monitoring check. Test alerts are always labeled TEST DATA."}
                  </EmptyState>
                </TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>
      </div>
    </AppShell>
  );
}
