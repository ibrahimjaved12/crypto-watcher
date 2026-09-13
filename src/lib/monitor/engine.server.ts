/**
 * Monitoring engine. Runs server-side (scheduled job or manual trigger) and is
 * the single place where threshold rules are evaluated.
 *
 * Designed to be extended in later milestones (indicators, news, email/WhatsApp
 * delivery) by adding evaluators alongside `evaluateThreshold`.
 */
import { getQuote } from "@/lib/market/quotes.server";
import { WINDOW_LABELS } from "@/lib/market/symbols";

export type MonitorSettings = {
  user_id: string;
  threshold_pct: number;
  window_minutes: number;
  cooldown_minutes: number;
  monitoring_enabled: boolean;
};

export const DEFAULT_SETTINGS = {
  threshold_pct: 2,
  window_minutes: 15,
  cooldown_minutes: 15,
  monitoring_enabled: true,
};

export type UserRunResult = {
  userId: string;
  status: "success" | "partial" | "failed" | "skipped";
  symbolsChecked: number;
  alertsCreated: number;
  dataSource: string | null;
  error: string | null;
};

type AdminClient = {
  from: (table: string) => any;
};

export function ruleLabel(thresholdPct: number, windowMinutes: number): string {
  const label = WINDOW_LABELS[windowMinutes] ?? `${windowMinutes}m`;
  return `abs(change) >= ${thresholdPct}% over ${label}`;
}

export async function runMonitorForUser(
  supabaseAdmin: AdminClient,
  userId: string,
  settings: MonitorSettings,
): Promise<UserRunResult> {
  const base: UserRunResult = {
    userId,
    status: "success",
    symbolsChecked: 0,
    alertsCreated: 0,
    dataSource: null,
    error: null,
  };

  if (!settings.monitoring_enabled) {
    return { ...base, status: "skipped" };
  }

  const { data: items, error: itemsError } = await supabaseAdmin
    .from("watchlist_items")
    .select("symbol")
    .eq("user_id", userId);

  if (itemsError) {
    return { ...base, status: "failed", error: itemsError.message };
  }

  const symbols: string[] = (items ?? []).map((i: { symbol: string }) => i.symbol);
  if (symbols.length === 0) return { ...base, status: "skipped" };

  const cooldownSince = new Date(
    Date.now() - settings.cooldown_minutes * 60 * 1000,
  ).toISOString();
  const rule = ruleLabel(settings.threshold_pct, settings.window_minutes);

  const { data: recent } = await supabaseAdmin
    .from("alerts")
    .select("symbol, rule, triggered_at")
    .eq("user_id", userId)
    .eq("rule", rule)
    .eq("is_test", false)
    .gte("triggered_at", cooldownSince);

  const onCooldown = new Set<string>(
    (recent ?? []).map((a: { symbol: string }) => a.symbol),
  );

  const failures: string[] = [];
  let alertsCreated = 0;
  let source: string | null = null;

  for (const symbol of symbols) {
    const quote = await getQuote(symbol);
    if (!quote.ok || quote.price == null) {
      failures.push(`${symbol}: ${quote.error ?? "unavailable"}`);
      continue;
    }
    source = source ?? quote.source;
    if (quote.stale) {
      failures.push(`${symbol}: stale data from ${quote.source}`);
      continue;
    }

    const change = quote.changes[settings.window_minutes];
    if (change == null) {
      failures.push(`${symbol}: no ${settings.window_minutes}m window available`);
      continue;
    }
    if (Math.abs(change) < settings.threshold_pct) continue;
    if (onCooldown.has(symbol)) continue;

    const { error: insertError } = await supabaseAdmin.from("alerts").insert({
      user_id: userId,
      symbol,
      change_pct: Number(change.toFixed(4)),
      window_minutes: settings.window_minutes,
      threshold_pct: settings.threshold_pct,
      rule,
      price: quote.price,
      data_source: quote.source,
      is_test: false,
    });

    if (insertError) {
      failures.push(`${symbol}: could not save alert (${insertError.message})`);
      continue;
    }
    onCooldown.add(symbol);
    alertsCreated += 1;
  }

  const status: UserRunResult["status"] =
    failures.length === 0 ? "success" : failures.length >= symbols.length ? "failed" : "partial";

  return {
    userId,
    status,
    symbolsChecked: symbols.length,
    alertsCreated,
    dataSource: source,
    error: failures.length ? failures.join(" | ") : null,
  };
}

export async function recordRun(
  supabaseAdmin: AdminClient,
  result: UserRunResult,
): Promise<void> {
  await supabaseAdmin.from("monitor_runs").insert({
    user_id: result.userId,
    status: result.status,
    symbols_checked: result.symbolsChecked,
    alerts_created: result.alertsCreated,
    data_source: result.dataSource,
    error_message: result.error,
  });
}
