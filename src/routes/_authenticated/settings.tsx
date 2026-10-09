import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { humanizeReason } from "@/lib/labels";
import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { fetchSettings, saveSettings } from "@/lib/db";
import { timestampFreshness } from "@/lib/freshness";
import { getOperationalState } from "@/lib/operational.functions";
import type { CollectorHealthStatus } from "@/lib/operational/types";
import { useServerFn } from "@tanstack/react-start";

export const Route = createFileRoute("/_authenticated/settings")({
  head: () => ({
    meta: [
      { title: "Control room — Crypto Watch" },
      {
        name: "description",
        content:
          "Control market collection, movement alerts and technical analysis, and review recent checks.",
      },
      { property: "og:title", content: "Control room — Crypto Watch" },
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
  return milliseconds === null
    ? "Unknown delay"
    : `${Math.max(0, milliseconds / 1_000).toFixed(1)} s behind`;
}

function HealthChip({ status, lag }: { status: string; lag?: number | null }) {
  const live = status === "LIVE" || status === "FRESH";
  const offline = status === "UNAVAILABLE";
  return (
    <Badge
      variant="outline"
      className={
        live
          ? "border-bull/40 text-bull"
          : offline
            ? "border-bear/40 text-bear"
            : "border-warn/40 text-warn"
      }
    >
      {live ? "Live" : offline ? "Offline" : "Lagging"}
      {lag !== undefined ? ` · ${formatLag(lag)}` : ""}
    </Badge>
  );
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
    onError: (e: Error) => toast.error(humanizeReason(e.message).short),
  });

  return (
    <AppShell>
      <h1 className="text-2xl font-semibold">Control room</h1>
      <p className="text-sm text-muted-foreground">
        Set your alerts, choose what to collect and check that the data is arriving.
      </p>
      {settings.isPending && (
        <p role="status" className="mt-3 text-sm text-muted-foreground">
          Loading your settings…
        </p>
      )}
      {settings.error && (
        <div role="alert" className="mt-3 text-sm text-bear">
          {humanizeReason(settings.error.message).short}
          <details className="text-xs">
            <summary>Details</summary>
            {settings.error.message}
          </details>
        </div>
      )}
      <form
        className="mt-5 space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <div className="grid min-w-0 gap-4 lg:grid-cols-2">
          <section className="panel min-w-0 space-y-4 p-5" aria-labelledby="alert-settings-title">
            <h2 id="alert-settings-title" className="text-lg font-semibold">
              Alerts
            </h2>
            <p className="text-sm text-muted-foreground">
              Choose how far a price must move before you hear about it.
            </p>
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
                Alert on a rise or fall of this percentage from your saved reference price, even if
                it takes longer than 15 minutes.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="cooldown">Wait between alerts in the same direction (minutes)</Label>
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
                An upward alert does not block a downward alert; the reference price stays fixed
                while you wait.
              </p>
            </div>

            <div className="flex items-center justify-between gap-4 border-t border-border/70 pt-3">
              <div>
                <Label htmlFor="movement-alerts">Price movement alerts</Label>
                <p className="text-xs text-muted-foreground">
                  Saves an alert when a price crosses your threshold and the wait between alerts has
                  ended.
                </p>
              </div>
              <Switch
                id="movement-alerts"
                checked={movementAlertsEnabled}
                onCheckedChange={setMovementAlertsEnabled}
              />
            </div>

            <details className="rounded-lg border border-border px-3 text-xs text-muted-foreground">
              <summary>How the reference price works</summary>
              <p className="text-muted-foreground">
                The first successful check sets a reference price for each pair. Each saved alert
                resets it to the alert price. At 2%, a reference of 100 USDT alerts at 102 or 98
                USDT. Small moves are retained between checks.
              </p>
              <p className="text-xs text-muted-foreground">
                Uses completed one-minute candles. Changing the threshold or data source starts a
                new reference on the next fresh check. Dashboard percentage windows are separate
                from this alert rule.
              </p>
            </details>
          </section>
          <section className="panel min-w-0 space-y-4 p-5" aria-labelledby="collection-title">
            <h2 id="collection-title" className="text-lg font-semibold">
              Data collection
            </h2>
            <p className="text-sm text-muted-foreground">
              These switches apply to scheduled monitoring and “Run check now”.
            </p>
            <div className="flex items-center justify-between gap-4 rounded-md border border-border p-3">
              <div>
                <Label htmlFor="enabled">Monitoring</Label>
                <p className="text-xs text-muted-foreground">
                  Pauses monitoring, including price alerts, while keeping your settings and saved
                  history.
                </p>
              </div>
              <Switch id="enabled" checked={enabled} onCheckedChange={setEnabled} />
            </div>

            <div className="space-y-4">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <Label htmlFor="market-data">Market-data collection</Label>
                  <p className="text-xs text-muted-foreground">
                    Saves the latest completed one-minute price candle; turning this off also pauses
                    movement alerts and indicator analysis.
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
                  <Label htmlFor="technical-analysis">Save indicator analysis</Label>
                  <p className="text-xs text-muted-foreground">
                    Saves 15-minute, 1-hour and 4-hour market reads and checks what happened next.
                  </p>
                </div>
                <Switch
                  id="technical-analysis"
                  checked={technicalAnalysisEnabled}
                  onCheckedChange={setTechnicalAnalysisEnabled}
                />
              </div>
            </div>

            <details className="rounded-lg border border-dashed px-3 text-xs text-muted-foreground">
              <summary>Other activity controls</summary>
              <p>
                Per-account controls for developing setups, paper execution, email and WhatsApp
                delivery are not available yet. Strategy Lab runs independently through its own
                buttons and scheduled jobs.
              </p>
            </details>
          </section>
        </div>
        <Button type="submit" disabled={save.isPending || settings.isPending || settings.isError}>
          {save.isPending ? "Saving…" : "Save settings"}
        </Button>
        {save.error && (
          <details className="text-xs text-bear">
            <summary>Save error details</summary>
            {save.error.message}
          </details>
        )}
      </form>
      <section className="panel mt-6 min-w-0 p-5" aria-labelledby="data-health-title">
        <h2 id="data-health-title" className="text-lg font-semibold">
          Data health
        </h2>
        <p className="text-sm text-muted-foreground">
          Live means updates are arriving; lagging data needs time to catch up.
        </p>
        {operational.isPending && (
          <p role="status" className="mt-3 text-sm">
            Loading data health…
          </p>
        )}
        {operational.error && (
          <div role="alert" className="mt-3 text-sm text-bear">
            {humanizeReason(operational.error.message).short}
            <details className="text-xs">
              <summary>Details</summary>
              {operational.error.message}
            </details>
          </div>
        )}
        {overallCollectorStatus ? (
          <div className="mt-3 space-y-2 border-t border-border/60 pt-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <p className="text-sm font-medium">Binance price collection</p>
                <p className="num text-xs text-muted-foreground">
                  Stored price history: {operational.data?.collectorDiagnostics?.candle_rows ?? 0}{" "}
                  completed candles
                </p>
              </div>
              <HealthChip status={overallCollectorStatus} />
            </div>

            <div
              className="overflow-x-auto"
              tabIndex={0}
              role="region"
              aria-label="Market data health"
            >
              <table className="w-full min-w-[780px] text-left text-xs">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border/60">
                    <th className="py-2 pr-3 font-medium">Pair</th>
                    <th className="py-2 pr-3 font-medium">Interval</th>
                    <th className="py-2 pr-3 font-medium">Status</th>
                    <th className="py-2 pr-3 font-medium">Latest exchange update</th>
                    <th className="py-2 pr-3 font-medium">Last completed candle</th>
                    <th className="py-2 pr-3 font-medium">Delay</th>
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
                        <HealthChip status={row.status} lag={row.lag_ms} />
                      </td>
                      <td className="num py-2 pr-3">
                        {row.last_event_at ? new Date(row.last_event_at).toLocaleString() : "—"}
                      </td>
                      <td className="num py-2 pr-3">
                        {row.last_completed_open_time
                          ? new Date(row.last_completed_open_time).toLocaleString()
                          : "—"}
                      </td>
                      <td className="num py-2 pr-3">{formatLag(row.lag_ms)}</td>
                      <td className="num py-2">
                        {row.reconnect_count}
                        <details className="mt-1 break-words">
                          <summary>Details</summary>
                          {row.status} · {row.instrument_id}
                        </details>
                      </td>
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
                  {movementEngine.configuredSymbolCount} pairs configured ·{" "}
                  {movementEngine.eligibleSymbolCount} ready for analysis
                </p>
              </div>
              <HealthChip status={movementEngine.status} />
            </div>
            <details className="break-words">
              <summary>Technical details</summary>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-muted-foreground">Primary 5m</span>
                <span className="num ml-auto">
                  {movementEngine.primaryDirectionState} · {movementEngine.primaryPace}
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
                  {movementEngine.movementAlgorithmVersion} · {movementEngine.movementConfigVersion}{" "}
                  · {movementEngine.universeVersion}
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
            </details>
          </div>
        ) : null}
        {activityFreshness ? (
          <div className="mt-3 space-y-2 border-t border-border/60 pt-3 text-xs">
            <p className="text-sm font-medium">Recent successful updates</p>
            {collectorHealth.length === 0
              ? (() => {
                  const status = timestampFreshness(activityFreshness.marketCheckpoint.observedAt, {
                    available: activityFreshness.marketCheckpoint.available,
                    now: freshnessNow,
                    freshForMs: 10 * 60_000,
                    delayedForMs: 30 * 60_000,
                  });
                  return (
                    <div className="flex flex-wrap items-center gap-2">
                      <HealthChip status={status} />
                      <span className="text-muted-foreground">Latest saved price</span>
                      <span className="num ml-auto">
                        {activityFreshness.marketCheckpoint.observedAt
                          ? new Date(activityFreshness.marketCheckpoint.observedAt).toLocaleString()
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
                  <HealthChip status={status} />
                  <span className="text-muted-foreground">Price movement checked through</span>
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
                  <HealthChip status={status} />
                  <span className="text-muted-foreground">
                    Last successful {formatInterval(entry.timeframeMinutes)} indicator analysis
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

        {!operational.isPending &&
          !operational.error &&
          !overallCollectorStatus &&
          !activityFreshness && (
            <p className="py-5 text-sm text-muted-foreground">
              Data health will appear after collection starts and the first monitoring check
              completes.
            </p>
          )}
        <details className="mt-3 break-words text-xs text-muted-foreground">
          <summary>Storage details</summary>
          {operational.data?.diagnostics ? (
            <p className="num mt-1 text-xs text-muted-foreground">
              Request-driven operational storage: {operational.data.diagnostics.recent_candle_rows}{" "}
              completed candles · {operational.data.diagnostics.checkpoint_rows} checkpoints ·{" "}
              {operational.data.diagnostics.monitor_run_rows} runs ·{" "}
              {operational.data.diagnostics.pending_outbox_rows} pending sync ·{" "}
              {operational.data.diagnostics.failed_outbox_rows} failed ·{" "}
              {operational.data.diagnostics.dead_outbox_rows} dead-letter
            </p>
          ) : null}
        </details>
      </section>
      <section className="panel mt-6 min-w-0 p-5" aria-labelledby="run-history-title">
        <h2 id="run-history-title" className="text-lg font-semibold">
          Run history
        </h2>
        <p className="text-sm text-muted-foreground">
          Recent scheduled and manual monitoring checks, including anything that needs attention.
        </p>
        {operational.isPending && (
          <p className="mt-3 text-sm text-muted-foreground">Loading recent checks…</p>
        )}
        {operational.isError && (
          <p className="mt-3 text-sm text-muted-foreground">
            Run history could not be loaded. Refresh the page to try again.
          </p>
        )}
        <ul className="mt-4 space-y-2">
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
                  {humanizeReason(r.status).short}
                </Badge>
                <span className="num text-xs text-muted-foreground">
                  {new Date(r.ran_at).toLocaleString()}
                </span>
                <span className="num ml-auto text-xs text-muted-foreground">
                  {r.symbols_checked} pairs checked · {r.alerts_created} alerts
                  {r.data_source ? ` · ${humanizeReason(r.data_source).short}` : ""}
                </span>
              </div>
              <details className="mt-2 text-xs text-muted-foreground">
                <summary>Performance details</summary>
                <p className="num">
                  {r.duration_ms ?? 0} ms · {r.metrics.exchangeRequests ?? 0} exchange requests ·{" "}
                  {r.metrics.candleRows ?? 0} candle rows · {r.metrics.taCalculations ?? 0} TA
                  calculations · {r.metrics.taSignalsSaved ?? 0} TA signals ·{" "}
                  {r.metrics.taOutcomesUpdated ?? 0} outcomes · {r.metrics.databaseReads ?? 0} DB
                  reads · {r.metrics.databaseWriteAttempts ?? 0} DB write attempts ·{" "}
                  {r.metrics.databaseNoOps ?? 0} no-ops · {r.metrics.marketCacheHits ?? 0} shared
                  inputs
                </p>
                <p className="mt-2">
                  Raw status: {r.status} · source: {r.data_source ?? "none"}
                </p>
              </details>
              {r.error_message ? (
                <div className="mt-2 text-xs">
                  <p className="text-warn">{humanizeReason(r.error_message).short}</p>
                  <p className="mt-1 text-muted-foreground">
                    {humanizeReason(r.error_message).help}
                  </p>
                  <details className="break-words text-muted-foreground">
                    <summary>Details</summary>
                    {r.error_message}
                  </details>
                </div>
              ) : null}
            </li>
          ))}
          {runs.data?.length === 0 ? (
            <li className="text-sm text-muted-foreground">
              No checks recorded yet. Run a check from Market or wait for the next scheduled check
              to see results here.
            </li>
          ) : null}
        </ul>
      </section>
    </AppShell>
  );
}
