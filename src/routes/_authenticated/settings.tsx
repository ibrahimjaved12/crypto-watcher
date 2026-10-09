import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { History } from "lucide-react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Hint } from "@/components/hint";
import { EmptyState, PageHeader, SectionTitle, StatusChip, TimeAgo } from "@/components/plain";
import {
  collectorLabel,
  freshnessLabel,
  humanizeReason,
  humanizeToken,
  relativeTime,
  sourceLabel,
  summarizeReasons,
} from "@/lib/labels";
import { fetchSettings, saveSettings } from "@/lib/db";
import { timestampFreshness, type FreshnessState } from "@/lib/freshness";
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
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <AppShell>
      <PageHeader
        eyebrow="Settings & health"
        title="Control room"
        subtitle="Choose when you get alerts and what is collected, and check that everything is running."
      />

      <div className="mt-5 grid gap-4 lg:grid-cols-2">
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <section className="panel space-y-5 p-5">
            <SectionTitle
              title="Alerts"
              hint="When a price move is big enough to be saved as an alert."
            />
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
                Alert when price rises or falls this much from its saved starting price, however long
                the move takes.
              </p>
              <details className="text-xs text-muted-foreground">
                <summary>How the starting price works</summary>
                <div className="mt-2 space-y-2">
                  <p>
                    The first successful check saves a starting price (baseline) for each pair. Each saved
                    alert resets it to the alert price. At 2%, a starting price of 100 USDT alerts at 102 or
                    98 USDT. Small moves add up between checks.
                  </p>
                  <p>
                    Uses completed one-minute candles. Changing the threshold or data source starts a new
                    starting price on the next fresh check. The percentage moves on the Market page are
                    separate from this rule.
                  </p>
                </div>
              </details>
            </div>

            <div className="space-y-2">
              <Label htmlFor="cooldown">Quiet time per pair and direction (minutes)</Label>
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
                After an alert, the same pair and direction stays quiet this long. An up alert never
                blocks a down alert.
              </p>
            </div>
          </section>

          <section className="panel space-y-4 p-5">
            <SectionTitle
              title="Data collection"
              hint="Each switch is independent; the master switch pauses all of them without losing their state."
            />
            <SwitchRow
              id="enabled"
              label="Monitoring master switch"
              what="Turns every activity below on or off at once."
              checked={enabled}
              onChange={setEnabled}
            />
            <SwitchRow
              id="market-data"
              label="Market-data collection"
              what="Fetches the latest completed one-minute candle for your pairs. Turning it off also pauses the two below."
              checked={marketDataEnabled}
              onChange={setMarketDataEnabled}
            />
            <SwitchRow
              id="movement-alerts"
              label="Movement alerts"
              what="Compares each new price with your threshold and quiet time, and saves alerts that qualify."
              checked={movementAlertsEnabled}
              onChange={setMovementAlertsEnabled}
            />
            <SwitchRow
              id="technical-analysis"
              label="Technical analysis snapshots"
              what="Saves 15m, 1h and 4h indicator readings (shown under Indicator history) and later fills in what happened next."
              checked={technicalAnalysisEnabled}
              onChange={setTechnicalAnalysisEnabled}
            />
            <details className="text-xs text-muted-foreground">
              <summary>Planned activities (not available yet)</summary>
              <ul className="mt-2 space-y-1">
                {[
                  "Developing-setup and strategy evaluation",
                  "Paper-trading execution",
                  "Email notification delivery",
                  "WhatsApp notification delivery",
                ].map((label) => (
                  <li key={label} className="flex items-center justify-between gap-3">
                    <span>{label}</span>
                    <Badge variant="secondary">Planned</Badge>
                  </li>
                ))}
              </ul>
            </details>
          </section>

          <Button type="submit" className="w-full sm:w-auto" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save settings"}
          </Button>
        </form>

        <div className="min-w-0 space-y-4">
          <section className="panel min-w-0 p-5">
            <SectionTitle
              title="Data health"
              hint="Is live market data arriving? Lag is how far the newest stored data is behind now."
              right={
                overallCollectorStatus ? (
                  <StatusChip level={collectorLabel(overallCollectorStatus).level}>
                    {collectorLabel(overallCollectorStatus).short}
                  </StatusChip>
                ) : null
              }
            />
            {collectorHealth.length ? (
              <div className="-mx-1 overflow-x-auto px-1" tabIndex={0} aria-label="Data feed per pair">
                <table className="w-full min-w-[460px] text-left text-xs">
                  <thead className="text-muted-foreground">
                    <tr className="border-b border-border/60">
                      <th className="py-2 pr-3 font-medium">Pair</th>
                      <th className="py-2 pr-3 font-medium">Candles</th>
                      <th className="py-2 pr-3 font-medium">Feed</th>
                      <th className="py-2 pr-3 font-medium">Lag</th>
                      <th className="py-2 pr-3 font-medium">Last update</th>
                      <th className="py-2 font-medium">
                        <Hint text="How often the live connection had to reconnect. A few is normal.">
                          Reconnects
                        </Hint>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {collectorHealth.map((row) => {
                      const state = collectorLabel(row.status);
                      return (
                        <tr
                          key={`${row.instrument_id}:${row.timeframe_minutes}`}
                          className="border-b border-border/40 last:border-0"
                        >
                          <td className="py-2 pr-3 font-medium">{row.symbol.replace(/USDT$/, "")}</td>
                          <td className="py-2 pr-3">{formatInterval(row.timeframe_minutes)}</td>
                          <td className="py-2 pr-3">
                            <StatusChip level={state.level}>{state.short}</StatusChip>
                          </td>
                          <td className="num py-2 pr-3">{formatLag(row.lag_ms)}</td>
                          <td className="py-2 pr-3">
                            <TimeAgo
                              at={row.last_event_at}
                              text={relativeTime(row.last_event_at)}
                            />
                          </td>
                          <td className="num py-2">{row.reconnect_count}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                No live data feed is reporting yet. Rows appear once the collector starts storing candles
                for your pairs.
              </p>
            )}

            {movementEngine ? (
              <div className="mt-4 space-y-1 border-t border-border/60 pt-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">Market movement detector</span>
                  <StatusChip level={collectorLabel(movementEngine.status).level}>
                    {collectorLabel(movementEngine.status).short}
                  </StatusChip>
                </div>
                <p className="text-xs text-muted-foreground">
                  Watching {movementEngine.eligibleSymbolCount} of {movementEngine.configuredSymbolCount}{" "}
                  coins · 5m market: {humanizeToken(movementEngine.primaryDirectionState)},{" "}
                  {humanizeToken(movementEngine.primaryPace).toLowerCase()} · checked{" "}
                  {relativeTime(movementEngine.lastEvaluationBoundaryTime)}
                </p>
                <details className="text-xs text-muted-foreground">
                  <summary>Technical details</summary>
                  <dl className="num mt-2 space-y-1">
                    <Row name="Most recent transition">
                      {movementEngine.mostRecentTransition
                        ? `${movementEngine.mostRecentTransition.transition} · ${movementEngine.mostRecentTransition.transitionReason}`
                        : "—"}
                    </Row>
                    <Row name="Algorithm / config / universe">
                      {movementEngine.movementAlgorithmVersion} · {movementEngine.movementConfigVersion} ·{" "}
                      {movementEngine.universeVersion}
                    </Row>
                    <Row name="Finalization config / grace">
                      {movementEngine.finalizationConfigVersion ?? "—"}
                      {movementEngine.finalizationGraceMs === null
                        ? ""
                        : ` · ${movementEngine.finalizationGraceMs}ms`}
                    </Row>
                    <Row name="Late after finalization">{movementEngine.lateAfterFinalizationCount}</Row>
                    <Row name="Last evaluation boundary">
                      {new Date(movementEngine.lastEvaluationBoundaryTime).toLocaleString()}
                    </Row>
                  </dl>
                </details>
              </div>
            ) : null}

            {activityFreshness ? (
              <div className="mt-4 space-y-2 border-t border-border/60 pt-3 text-xs">
                <p className="text-sm font-medium">Latest results</p>
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
                        <FreshRow
                          status={status}
                          name="Latest market price saved"
                          at={activityFreshness.marketCheckpoint.observedAt}
                        />
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
                    <FreshRow
                      status={status}
                      name="Movement checked up to"
                      at={activityFreshness.movement.evaluatedThrough}
                    />
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
                    <FreshRow
                      key={entry.timeframeMinutes}
                      status={status}
                      name={`Last ${formatInterval(entry.timeframeMinutes)} indicator snapshot`}
                      at={entry.evaluatedAt}
                      extra={
                        entry.completedCandleAt
                          ? `candle ${new Date(entry.completedCandleAt).toLocaleString()}`
                          : undefined
                      }
                    />
                  );
                })}
              </div>
            ) : null}

            {operational.data?.diagnostics || operational.data?.collectorDiagnostics ? (
              <details className="mt-4 border-t border-border/60 pt-3 text-xs text-muted-foreground">
                <summary>Storage details</summary>
                <p className="num mt-2">
                  {operational.data?.diagnostics
                    ? `${operational.data.diagnostics.recent_candle_rows} completed candles · ${operational.data.diagnostics.checkpoint_rows} checkpoints · ${operational.data.diagnostics.monitor_run_rows} runs · ${operational.data.diagnostics.pending_outbox_rows} pending sync · ${operational.data.diagnostics.failed_outbox_rows} failed · ${operational.data.diagnostics.dead_outbox_rows} dead-letter`
                    : null}
                </p>
                {operational.data?.collectorDiagnostics ? (
                  <p className="num mt-1">
                    Collector storage: {operational.data.collectorDiagnostics.candle_rows ?? 0} completed
                    candles
                  </p>
                ) : null}
              </details>
            ) : null}
          </section>

          <section className="panel p-5">
            <SectionTitle
              title="Run history"
              hint="Every scheduled and manual alert check, including failures."
            />
            <ul className="space-y-2">
              {(runs.data ?? []).map((r) => {
                const status = humanizeReason(r.status);
                const error = r.error_message ? summarizeReasons(r.error_message) : null;
                return (
                  <li key={r.id} className="rounded-md border border-border/70 p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusChip level={status.level ?? "wait"}>{status.short}</StatusChip>
                      <TimeAgo at={r.ran_at} text={relativeTime(r.ran_at)} />
                      <span className="ml-auto text-xs text-muted-foreground">
                        {r.symbols_checked} pairs checked, {r.alerts_created} alert
                        {r.alerts_created === 1 ? "" : "s"}
                        {r.data_source ? ` · ${sourceLabel(r.data_source)}` : ""}
                      </span>
                    </div>
                    {error ? (
                      <ul className="mt-2 space-y-0.5 text-xs text-bear">
                        {error.items.slice(0, 4).map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                        {error.items.length > 4 ? (
                          <li className="text-muted-foreground">…and {error.items.length - 4} more</li>
                        ) : null}
                      </ul>
                    ) : null}
                    <details className="mt-2 text-xs text-muted-foreground">
                      <summary>Performance details</summary>
                      <p className="num mt-1">
                        {r.duration_ms ?? 0} ms · {r.metrics.exchangeRequests ?? 0} exchange requests ·{" "}
                        {r.metrics.candleRows ?? 0} candle rows · {r.metrics.taCalculations ?? 0} TA
                        calculations · {r.metrics.taSignalsSaved ?? 0} TA signals ·{" "}
                        {r.metrics.taOutcomesUpdated ?? 0} outcomes · {r.metrics.databaseReads ?? 0} DB
                        reads · {r.metrics.databaseWriteAttempts ?? 0} DB write attempts ·{" "}
                        {r.metrics.databaseNoOps ?? 0} no-ops · {r.metrics.marketCacheHits ?? 0} shared
                        inputs
                      </p>
                      {r.error_message ? (
                        <code className="mt-2 block break-words rounded bg-muted/60 p-2 font-mono text-[11px]">
                          {r.status} · {r.error_message}
                        </code>
                      ) : null}
                    </details>
                  </li>
                );
              })}
            </ul>
            {runs.data?.length === 0 ? (
              <EmptyState
                icon={History}
                title="No checks recorded yet"
                body="Scheduled checks run every few minutes while monitoring is on; “Check for alerts now” on the Market page runs one immediately."
                className="py-6"
              />
            ) : null}
          </section>
        </div>
      </div>
    </AppShell>
  );
}

function SwitchRow({
  id,
  label,
  what,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  what: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-4 border-t border-border/60 pt-3 first-of-type:border-0 first-of-type:pt-0">
      <div>
        <Label htmlFor={id}>{label}</Label>
        <p className="mt-0.5 text-xs text-muted-foreground">{what}</p>
      </div>
      <Switch id={id} checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

function Row({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap gap-2">
      <dt>{name}</dt>
      <dd className="ml-auto text-right">{children}</dd>
    </div>
  );
}

function FreshRow({
  status,
  name,
  at,
  extra,
}: {
  status: FreshnessState;
  name: string;
  at: string | number | null | undefined;
  extra?: string | undefined;
}) {
  const label = freshnessLabel(status);
  return (
    <div className="flex flex-wrap items-center gap-2">
      <StatusChip level={label.level}>{label.short}</StatusChip>
      <span className="text-muted-foreground">{name}</span>
      <span className="ml-auto" title={extra}>
        {at ? <TimeAgo at={at} text={relativeTime(at)} /> : "—"}
      </span>
    </div>
  );
}
