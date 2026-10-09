import { PageHeader, TechnicalDetails, QueryNotice } from "@/components/presentation";
import {
  monitoringRunLabel,
  collectorStatusLabel,
  freshnessLabel,
  sourceLabel,
  issueSummary,
} from "@/lib/presentation/labels";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { fetchSettings, saveSettings } from "@/lib/db";
import { timestampFreshness, type FreshnessState } from "@/lib/freshness";
import { getOperationalState } from "@/lib/operational.functions";
import type { CollectorHealthStatus } from "@/lib/operational/types";
import type { MovementEngineStatus } from "@/lib/market/market-movement-state";
import { useServerFn } from "@tanstack/react-start";
import { humanizeCode, humanizeReason } from "@/lib/labels";

/** LIVE / RECOVERING / STALE / UNAVAILABLE -> a short chip with the lag, colour plus words. */
function CollectorChip({ status, lagMs }: { status: CollectorHealthStatus; lagMs: number | null }) {
  const [text, tone] =
    status === "LIVE"
      ? ["Live", "bull"]
      : status === "UNAVAILABLE"
        ? ["Offline", "bear"]
        : ["Lagging", "warn"];
  return (
    <span className="chip" data-tone={tone} title={status}>
      {text}
      {lagMs !== null && status !== "UNAVAILABLE" ? ` · ${formatLag(lagMs)} behind` : ""}
    </span>
  );
}

export const Route = createFileRoute("/_authenticated/settings")({
  head: () => ({
    meta: [
      { title: "Monitoring settings — Crypto Watch" },
      {
        name: "description",
        content:
          "Control market collection, movement alerts and technical analysis, and review recent checks.",
      },
      { property: "og:title", content: "Monitoring settings — Crypto Watch" },
      {
        property: "og:description",
        content: "Independent monitoring activities, cumulative alerts and run history.",
      },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: SettingsPage,
});

function formatInterval(minutes: number) {
  return minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`;
}

function formatLag(milliseconds: number | null) {
  if (milliseconds === null) return "—";
  if (milliseconds < 1_000) return `${milliseconds}ms`;
  if (milliseconds < 60_000) return `${Math.round(milliseconds / 1_000)}s`;
  return `${Math.round(milliseconds / 60_000)}m`;
}

function collectorOverallStatus(
  health: Array<{ status: CollectorHealthStatus }>,
): CollectorHealthStatus | null {
  if (health.length === 0) return null;
  if (health.some((entry) => entry.status === "UNAVAILABLE")) return "UNAVAILABLE";
  if (health.some((entry) => entry.status === "STALE")) return "STALE";
  if (health.some((entry) => entry.status === "RECOVERING")) return "RECOVERING";
  return "LIVE";
}

function collectorBadgeVariant(status: CollectorHealthStatus) {
  if (status === "UNAVAILABLE") return "destructive" as const;
  if (status === "LIVE") return "secondary" as const;
  return "outline" as const;
}

function movementBadgeVariant(status: MovementEngineStatus) {
  if (status === "UNAVAILABLE") return "destructive" as const;
  if (status === "LIVE") return "secondary" as const;
  return "outline" as const;
}

function freshnessBadgeVariant(status: FreshnessState) {
  if (status === "UNAVAILABLE") return "destructive" as const;
  if (status === "FRESH") return "secondary" as const;
  return "outline" as const;
}

function SettingsPage() {
  const loadOperationalState = useServerFn(getOperationalState);
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const operational = useQuery({
    queryKey: ["operational-state"],
    queryFn: () => loadOperationalState(),
  });
  const runs = { data: operational.data?.runs };
  const collectorHealth = operational.data?.collectorHealth ?? [];
  const overallCollectorStatus = collectorOverallStatus(collectorHealth);
  const movementEngine = operational.data?.movementEngine ?? null;
  const activityFreshness = operational.data?.activityFreshness;
  const freshnessNow = operational.dataUpdatedAt || Date.now();

  const [threshold, setThreshold] = useState("2");
  const [cooldown, setCooldown] = useState("15");
  const [enabled, setEnabled] = useState(true);
  const [marketDataEnabled, setMarketDataEnabled] = useState(true);
  const [technicalAnalysisEnabled, setTechnicalAnalysisEnabled] = useState(true);
  const [movementAlertsEnabled, setMovementAlertsEnabled] = useState(true);

  useEffect(() => {
    if (!settings.data) return;
    setThreshold(String(settings.data.threshold_pct));
    setCooldown(String(settings.data.cooldown_minutes));
    setEnabled(settings.data.monitoring_enabled);
    setMarketDataEnabled(settings.data.market_data_collection_enabled ?? true);
    setTechnicalAnalysisEnabled(settings.data.completed_candle_ta_enabled ?? true);
    setMovementAlertsEnabled(settings.data.movement_alerts_enabled ?? true);
  }, [settings.data]);

  const save = useMutation({
    mutationFn: () =>
      saveSettings({
        threshold_pct: Number(threshold),
        cooldown_minutes: Number(cooldown),
        monitoring_enabled: enabled,
        market_data_collection_enabled: marketDataEnabled,
        completed_candle_ta_enabled: technicalAnalysisEnabled,
        movement_alerts_enabled: movementAlertsEnabled,
      }),
    onSuccess: () => {
      toast.success("Settings saved.");
      queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e: Error) => toast.error(issueSummary(e.message)),
  });

  return (
    <AppShell>
      <PageHeader
        title="Settings"
        description="Set your monitoring preferences and see whether each part of the system is keeping up."
      />
      <QueryNotice pending={settings.isPending} error={settings.error} />
      {save.error && <TechnicalDetails>{save.error.message}</TechnicalDetails>}

      <div className="mt-5 grid items-start gap-6 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <form
          className="panel space-y-5 p-5"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <h2 className="text-lg font-semibold">Alerts</h2>
          <p className="text-sm text-muted-foreground">
            When a watched pair moves enough, an alert is saved. Applies to manual and scheduled
            checks.
          </p>
          <fieldset disabled={settings.isPending || settings.isError} className="min-w-0 space-y-5">
            <div className="space-y-2">
              <Label htmlFor="threshold">Alert threshold (%)</Label>
              <Input
                id="threshold"
                type="number"
                step="0.1"
                min="0.1"
                max="100"
                required
                value={threshold}
                onChange={(e) => setThreshold(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Alert on a rise or fall of this percentage from the saved baseline, even when the
                move takes longer than 15 minutes.
              </p>
            </div>

            <div className="space-y-2 rounded-md border border-border p-3 text-sm">
              <p className="font-medium">Comparison: saved baseline</p>
              <p className="text-muted-foreground">
                The first successful check sets a baseline for each pair. Each saved alert resets it
                to the alert price. At 2%, a baseline of 100 USDT alerts at 102 or 98 USDT. Small
                moves are retained between checks.
              </p>
              <p className="text-xs text-muted-foreground">
                Uses completed one-minute candles. Changing the threshold or data source starts a
                new baseline on the next fresh check. Dashboard percentage windows are separate from
                this alert rule.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="cooldown">Cooldown per pair and direction (minutes)</Label>
              <Input
                id="cooldown"
                type="number"
                min="1"
                max="1440"
                required
                value={cooldown}
                onChange={(e) => setCooldown(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                An upward alert does not block a downward alert. During cooldown the baseline stays
                fixed; a further qualifying move can alert on a fresh check after cooldown.
              </p>
            </div>

            <div className="flex items-center justify-between rounded-md border border-border p-3">
              <div>
                <Label htmlFor="enabled">Monitoring on/off</Label>
                <p className="text-xs text-muted-foreground">
                  Pauses everything below at once. Your individual switches and saved state are
                  kept.
                </p>
              </div>
              <Switch id="enabled" checked={enabled} onCheckedChange={setEnabled} />
            </div>

            <fieldset className="space-y-3 rounded-md border border-border p-3">
              <legend className="px-1 text-sm font-medium">Data collection</legend>

              <div className="flex items-center justify-between gap-4">
                <div>
                  <Label htmlFor="market-data">Collect market prices</Label>
                  <p className="text-xs text-muted-foreground">
                    Stores the latest finished one-minute candle for each pair. Everything else
                    depends on it, so turning it off pauses the two switches below too.
                  </p>
                </div>
                <Switch
                  id="market-data"
                  checked={marketDataEnabled}
                  onCheckedChange={setMarketDataEnabled}
                />
              </div>

              <div className="flex items-center justify-between gap-4 border-t border-border/70 pt-3">
                <div>
                  <Label htmlFor="movement-alerts">Price-move alerts</Label>
                  <p className="text-xs text-muted-foreground">
                    Saves an alert when a pair moves past your threshold, respecting the cooldown.
                  </p>
                </div>
                <Switch
                  id="movement-alerts"
                  checked={movementAlertsEnabled}
                  onCheckedChange={setMovementAlertsEnabled}
                />
              </div>

              <div className="flex items-center justify-between gap-4 border-t border-border/70 pt-3">
                <div>
                  <Label htmlFor="technical-analysis">Indicator readings</Label>
                  <p className="text-xs text-muted-foreground">
                    Saves the indicator history (15-minute, 1-hour and 4-hour) shown on the Market
                    page, and fills in what happened next.
                  </p>
                </div>
                <Switch
                  id="technical-analysis"
                  checked={technicalAnalysisEnabled}
                  onCheckedChange={setTechnicalAnalysisEnabled}
                />
              </div>
            </fieldset>

            <section className="space-y-3 rounded-md border border-dashed border-border p-3">
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-medium">Future activities</p>
                <Badge variant="outline">Not available yet</Badge>
              </div>
              <p className="text-xs text-muted-foreground">
                Automatic monitoring controls for these activities are not available yet. Signal and
                paper evaluations remain accessible in Strategy Lab.
              </p>
              {[
                "Developing-setup and strategy evaluation",
                "Paper-trading execution",
                "Email notification delivery",
                "WhatsApp notification delivery",
              ].map((label) => (
                <div
                  key={label}
                  className="flex items-center justify-between gap-3 border-t border-border/70 pt-3 text-sm"
                >
                  <span>{label}</span>
                  <Badge variant="secondary">Planned</Badge>
                </div>
              ))}
            </section>

            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save preferences"}
            </Button>
          </fieldset>
        </form>

        <section className="panel p-5">
          <h2 className="text-lg font-semibold">Data health</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Is market data arriving on time, and did the latest checks work?
          </p>
          <QueryNotice pending={operational.isPending} error={operational.error} />
          {!operational.isPending && !operational.error && (
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <div className="rounded-lg border p-4">
                <p className="text-xs text-muted-foreground">Market collector</p>
                <p className="mt-2 font-medium">{collectorStatusLabel(overallCollectorStatus)}</p>
              </div>
              <div className="rounded-lg border p-4">
                <p className="text-xs text-muted-foreground">Movement engine</p>
                <p className="mt-2 font-medium">{collectorStatusLabel(movementEngine?.status)}</p>
              </div>
              <div className="rounded-lg border p-4 sm:col-span-2">
                <p className="text-xs text-muted-foreground">Last monitoring run</p>
                <p className="mt-2 font-medium">
                  {runs.data?.[0]
                    ? monitoringRunLabel(runs.data[0].status)
                    : "No checks recorded yet"}
                </p>
                {runs.data?.[0] && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    {new Date(runs.data[0].ran_at).toLocaleString()}
                  </p>
                )}
              </div>
            </div>
          )}
          <TechnicalDetails title="Advanced diagnostics">
            {operational.data?.diagnostics ? (
              <p className="num mt-1 text-xs text-muted-foreground">
                Request-driven operational storage:{" "}
                {operational.data.diagnostics.recent_candle_rows} completed candles ·{" "}
                {operational.data.diagnostics.checkpoint_rows} checkpoints ·{" "}
                {operational.data.diagnostics.monitor_run_rows} runs ·{" "}
                {operational.data.diagnostics.pending_outbox_rows} pending sync ·{" "}
                {operational.data.diagnostics.failed_outbox_rows} failed ·{" "}
                {operational.data.diagnostics.dead_outbox_rows} dead-letter
              </p>
            ) : null}
            {overallCollectorStatus ? (
              <div className="mt-3 space-y-2 border-t border-border/60 pt-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="text-sm font-medium">Shared Binance collector</p>
                    <p className="num text-xs text-muted-foreground">
                      Collector-owned storage:{" "}
                      {operational.data?.collectorDiagnostics?.candle_rows ?? 0} completed candles
                    </p>
                  </div>
                  <Badge variant={collectorBadgeVariant(overallCollectorStatus)}>
                    {overallCollectorStatus}
                  </Badge>
                </div>

                <div className="overflow-x-auto">
                  <table className="w-full min-w-[760px] text-left text-xs">
                    <thead className="text-muted-foreground">
                      <tr className="border-b border-border/60">
                        <th className="py-2 pr-3 font-medium">Pair</th>
                        <th className="py-2 pr-3 font-medium">Candle</th>
                        <th className="py-2 pr-3 font-medium">Status</th>
                        <th className="py-2 pr-3 font-medium">Last message from exchange</th>
                        <th className="py-2 pr-3 font-medium">Last finished candle</th>
                        <th className="py-2 font-medium">Reconnects</th>
                      </tr>
                    </thead>
                    <tbody>
                      {collectorHealth.map((row) => (
                        <tr
                          key={`${row.instrument_id}:${row.timeframe_minutes}`}
                          className="border-b border-border/40 last:border-0"
                        >
                          <td className="py-2 pr-3 font-medium">{row.symbol}</td>
                          <td className="py-2 pr-3">{formatInterval(row.timeframe_minutes)}</td>
                          <td className="py-2 pr-3">
                            <CollectorChip status={row.status} lagMs={row.lag_ms} />
                          </td>
                          <td className="num py-2 pr-3">
                            {row.last_event_at ? new Date(row.last_event_at).toLocaleString() : "—"}
                          </td>
                          <td className="num py-2 pr-3">
                            {row.last_completed_open_time
                              ? new Date(row.last_completed_open_time).toLocaleString()
                              : "—"}
                          </td>
                          <td className="num py-2">{row.reconnect_count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : null}
            {movementEngine ? (
              <div className="mt-3 space-y-2 border-t border-border/60 pt-3 text-xs">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="text-sm font-medium">Market movement engine</p>
                    <p className="num text-muted-foreground">
                      {movementEngine.configuredSymbolCount} configured ·{" "}
                      {movementEngine.eligibleSymbolCount} eligible
                    </p>
                  </div>
                  <Badge variant={movementBadgeVariant(movementEngine.status)} title={movementEngine.status}>
                    {collectorStatusLabel(movementEngine.status)}
                  </Badge>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">Primary 5m</span>
                  <span className="num ml-auto">
                    {humanizeCode(movementEngine.primaryDirectionState)} ·{" "}
                    {humanizeCode(movementEngine.primaryPace)}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">Last evaluation boundary</span>
                  <span className="num ml-auto">
                    {new Date(movementEngine.lastEvaluationBoundaryTime).toLocaleString()}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">Most recent transition</span>
                  <span className="num ml-auto">
                    {movementEngine.mostRecentTransition
                      ? `${movementEngine.mostRecentTransition.transition} · ${movementEngine.mostRecentTransition.transitionReason}`
                      : "—"}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">Algorithm / config / universe</span>
                  <span className="num ml-auto">
                    {movementEngine.movementAlgorithmVersion} ·{" "}
                    {movementEngine.movementConfigVersion} · {movementEngine.universeVersion}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">Finalization config / grace</span>
                  <span className="num ml-auto">
                    {movementEngine.finalizationConfigVersion ?? "—"}
                    {movementEngine.finalizationGraceMs === null
                      ? ""
                      : ` · ${movementEngine.finalizationGraceMs}ms`}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">Late after finalization</span>
                  <span className="num ml-auto">{movementEngine.lateAfterFinalizationCount}</span>
                </div>
              </div>
            ) : null}
          </TechnicalDetails>
          {activityFreshness ? (
            <div className="mt-3 space-y-2 border-t border-border/60 pt-3 text-xs">
              <p className="text-sm font-medium">Analysis &amp; movement freshness</p>
              {collectorHealth.length === 0
                ? (() => {
                    const status = timestampFreshness(
                      activityFreshness.marketCheckpoint.observedAt,
                      {
                        available: activityFreshness.marketCheckpoint.available,
                        now: freshnessNow,
                        freshForMs: 10 * 60_000,
                        delayedForMs: 30 * 60_000,
                      },
                    );
                    return (
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant={freshnessBadgeVariant(status)}>
                          {freshnessLabel(status)}
                        </Badge>
                        <span className="text-muted-foreground">Latest market checkpoint</span>
                        <span className="num ml-auto">
                          {activityFreshness.marketCheckpoint.observedAt
                            ? new Date(
                                activityFreshness.marketCheckpoint.observedAt,
                              ).toLocaleString()
                            : "—"}
                        </span>
                      </div>
                    );
                  })()
                : null}
              {(() => {
                const status = timestampFreshness(activityFreshness.movement.evaluatedThrough, {
                  available: activityFreshness.movement.available,
                  now: freshnessNow,
                  freshForMs: 10 * 60_000,
                  delayedForMs: 30 * 60_000,
                });
                return (
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={freshnessBadgeVariant(status)}>{freshnessLabel(status)}</Badge>
                    <span className="text-muted-foreground">Movement evaluated through</span>
                    <span className="num ml-auto">
                      {activityFreshness.movement.evaluatedThrough
                        ? new Date(activityFreshness.movement.evaluatedThrough).toLocaleString()
                        : "—"}
                    </span>
                  </div>
                );
              })()}
              {activityFreshness.ta.map((entry) => {
                const duration = entry.timeframeMinutes * 60_000;
                const status = timestampFreshness(entry.evaluatedAt, {
                  available: entry.available,
                  now: freshnessNow,
                  freshForMs: duration * 2,
                  delayedForMs: duration * 3,
                });
                return (
                  <div key={entry.timeframeMinutes} className="flex flex-wrap items-center gap-2">
                    <Badge variant={freshnessBadgeVariant(status)}>{freshnessLabel(status)}</Badge>
                    <span className="text-muted-foreground">
                      Last successful {formatInterval(entry.timeframeMinutes)} analysis
                    </span>
                    <span className="num ml-auto">
                      {entry.evaluatedAt ? new Date(entry.evaluatedAt).toLocaleString() : "—"}
                      {entry.completedCandleAt
                        ? ` · candle ${new Date(entry.completedCandleAt).toLocaleString()}`
                        : ""}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : null}
          {!operational.isPending && !operational.error && !activityFreshness && (
            <p className="mt-4 text-sm text-muted-foreground">Analysis freshness is unavailable.</p>
          )}
          <h3 className="mt-6 font-semibold">Run history</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            Every manual and scheduled alert check, including the ones that failed.
          </p>
          <ul className="mt-4 space-y-3">
            {(runs.data ?? []).map((r) => (
              <li key={r.id} className="rounded-md border border-border/70 p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge
                    variant={
                      r.status === "success"
                        ? "secondary"
                        : r.status === "failed"
                          ? "destructive"
                          : "outline"
                    }
                  >
                    {monitoringRunLabel(r.status)}
                  </Badge>
                  <span className="num text-xs text-muted-foreground">
                    {new Date(r.ran_at).toLocaleString()}
                  </span>
                  <span className="ml-auto text-xs text-muted-foreground">
                    {r.symbols_checked} pairs checked, {r.alerts_created} alert
                    {r.alerts_created === 1 ? "" : "s"}
                    {r.data_source ? ` · ${sourceLabel(r.data_source)}` : ""}
                  </span>
                </div>
                {r.error_message && (
                  <div className="mt-2 text-xs">
                    <p className="text-warn">{humanizeReason(r.error_message).short}.</p>
                    {humanizeReason(r.error_message).help && (
                      <p className="mt-0.5 text-muted-foreground">
                        {humanizeReason(r.error_message).help}
                      </p>
                    )}
                  </div>
                )}
                <TechnicalDetails title="Performance and raw details">
                  <p>
                    Raw status: {r.status} · source: {r.data_source ?? "none"}
                  </p>
                  <p className="num mt-2 text-xs text-muted-foreground">
                    {r.duration_ms ?? 0} ms · {r.metrics.exchangeRequests ?? 0} exchange requests ·{" "}
                    {r.metrics.candleRows ?? 0} candle rows · {r.metrics.taCalculations ?? 0} TA
                    calculations · {r.metrics.taSignalsSaved ?? 0} TA signals ·{" "}
                    {r.metrics.taOutcomesUpdated ?? 0} outcomes · {r.metrics.databaseReads ?? 0} DB
                    reads · {r.metrics.databaseWriteAttempts ?? 0} DB write attempts ·{" "}
                    {r.metrics.databaseNoOps ?? 0} no-ops · {r.metrics.marketCacheHits ?? 0} shared
                    inputs
                  </p>
                  {r.error_message ? (
                    <p className="mt-2 text-xs text-destructive">{r.error_message}</p>
                  ) : null}
                </TechnicalDetails>
              </li>
            ))}
            {runs.data?.length === 0 ? (
              <li className="text-sm text-muted-foreground">
                No manual or scheduled monitoring runs recorded yet.
              </li>
            ) : null}
          </ul>
        </section>
      </div>
    </AppShell>
  );
}
