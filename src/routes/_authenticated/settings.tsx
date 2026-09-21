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
import { fetchRuns, fetchSettings, saveSettings } from "@/lib/db";

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

function SettingsPage() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const runs = useQuery({ queryKey: ["runs"], queryFn: fetchRuns });

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
      <h1 className="text-2xl font-semibold">Monitoring settings</h1>
      <p className="text-sm text-muted-foreground">
        Control each monitoring activity separately. These settings apply to scheduled runs and Run
        check now.
      </p>

      <div className="mt-5 grid gap-4 lg:grid-cols-2">
        <form
          className="panel space-y-5 p-5"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
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
              Alert on a rise or fall of this percentage from the saved baseline, even when the move
              takes longer than 15 minutes.
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
              Uses completed one-minute candles. Changing the threshold or data source starts a new
              baseline on the next fresh check. Dashboard percentage windows are separate from this
              alert rule.
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
              <Label htmlFor="enabled">Monitoring master switch</Label>
              <p className="text-xs text-muted-foreground">
                Pause every activity below without losing its individual setting or saved state.
              </p>
            </div>
            <Switch id="enabled" checked={enabled} onCheckedChange={setEnabled} />
          </div>

          <fieldset className="space-y-3 rounded-md border border-border p-3">
            <legend className="px-1 text-sm font-medium">Current monitoring activities</legend>

            <div className="flex items-center justify-between gap-4">
              <div>
                <Label htmlFor="market-data">Market-data collection</Label>
                <p className="text-xs text-muted-foreground">
                  Fetches and checkpoints the latest completed one-minute candle even if the two
                  activities below are paused. Turning this off also pauses both of them.
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
                <Label htmlFor="movement-alerts">Movement-alert generation</Label>
                <p className="text-xs text-muted-foreground">
                  Applies the saved-baseline threshold and cooldown, then saves qualifying alerts.
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
                <Label htmlFor="technical-analysis">Completed-candle technical analysis</Label>
                <p className="text-xs text-muted-foreground">
                  Saves 15m, 1h and 4h indicator snapshots and evaluates their pending outcomes.
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
              These remain off until their roadmap features exist. They are status rows, not working
              switches.
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
            Save settings
          </Button>
        </form>

        <section className="panel p-5">
          <h2 className="text-base font-semibold">Recent monitoring runs</h2>
          <p className="text-xs text-muted-foreground">
            Every scheduled and manual check, including failures.
          </p>
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
                    {r.status}
                  </Badge>
                  <span className="num text-xs text-muted-foreground">
                    {new Date(r.ran_at).toLocaleString()}
                  </span>
                  <span className="num ml-auto text-xs text-muted-foreground">
                    {r.symbols_checked} pairs · {r.alerts_created} alerts
                    {r.data_source ? ` · ${r.data_source}` : ""}
                  </span>
                </div>
                {r.error_message ? (
                  <p className="mt-2 text-xs text-destructive">{r.error_message}</p>
                ) : null}
              </li>
            ))}
            {runs.data?.length === 0 ? (
              <li className="text-sm text-muted-foreground">
                No runs recorded yet. The scheduler runs every 5 minutes.
              </li>
            ) : null}
          </ul>
        </section>
      </div>
    </AppShell>
  );
}
