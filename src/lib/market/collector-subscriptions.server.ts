import type { SupabaseClient } from "@supabase/supabase-js";
import type { Database } from "@/integrations/supabase/types";
import type { OperationalStore } from "../operational/types";
import { DEFAULT_SETTINGS } from "../monitor/engine.server";

type Client = Pick<SupabaseClient<Database>, "from">;

type Watcher = { user_id: string; symbol: string };

type CollectionSettings = {
  monitoring_enabled?: boolean | null;
  market_data_collection_enabled?: boolean | null;
};

export type CollectorUniverseSync =
  | { status: "assigned"; symbols: string[] }
  | { status: "unchanged"; symbols: string[] }
  | { status: "skipped"; reason: string };

/**
 * Derives the collector's shared subscription universe from Lovable-owned user
 * data. A symbol is included only while at least one account whose settings still
 * require market-data collection watches it (#15); duplicate watchers collapse to
 * one symbol, and removing or disabling the last relevant subscriber removes it.
 * The application owns watchlists and settings; this derived set is collector
 * input only, never a second user-data authority.
 */
export function collectorUniverse(
  watchers: readonly Watcher[],
  settings: ReadonlyMap<string, CollectionSettings>,
): string[] {
  const symbols = new Set<string>();
  for (const watcher of watchers) {
    const row = settings.get(watcher.user_id);
    // Documented defaults (#15/#22): monitoring and market-data collection are on
    // unless a user explicitly disabled them.
    const monitoring = row?.monitoring_enabled ?? DEFAULT_SETTINGS.monitoring_enabled;
    const collection =
      row?.market_data_collection_enabled ?? DEFAULT_SETTINGS.market_data_collection_enabled;
    if (!monitoring || !collection) continue;
    const symbol = watcher.symbol.trim().toUpperCase();
    if (symbol) symbols.add(symbol);
  }
  return [...symbols].sort();
}

/**
 * Application-owned reconciliation of the collector's subscription universe. It is
 * independent of scheduled monitoring: it runs on its own authenticated hook and at
 * application startup, so the collector cannot be left empty or stale by a disabled
 * monitor cron. The worker never reads Lovable itself.
 */
export async function syncCollectorUniverse(
  supabaseAdmin: Client,
  store: OperationalStore,
  env: Record<string, string | undefined> = process.env,
): Promise<CollectorUniverseSync> {
  if (env["BINANCE_COLLECTOR_ENABLED"] !== "true") {
    return { status: "skipped", reason: "Collector mode disabled" };
  }
  if (!store.enabled) {
    return { status: "skipped", reason: "Operational store disabled" };
  }

  const { data: watchers, error: watcherError } = await supabaseAdmin
    .from("watchlist_items")
    .select("user_id, symbol");
  if (watcherError) {
    throw new Error(`Collector universe watchlist read failed: ${watcherError.message}`);
  }
  const rows = (watchers ?? []) as Watcher[];
  const userIds = [...new Set(rows.map((row) => row.user_id))];

  const { data: settingsRows, error: settingsError } = await supabaseAdmin
    .from("monitor_settings")
    .select("user_id, monitoring_enabled, market_data_collection_enabled")
    .in("user_id", userIds.length ? userIds : ["00000000-0000-0000-0000-000000000000"]);
  if (settingsError) {
    throw new Error(`Collector universe settings read failed: ${settingsError.message}`);
  }
  const byUser = new Map<string, CollectionSettings>(
    (settingsRows ?? []).map((row) => [row.user_id, row as CollectionSettings]),
  );

  const symbols = collectorUniverse(rows, byUser);
  const current = await store.readCollectorSubscriptions();
  if (
    current.length === symbols.length &&
    current.every((symbol, index) => symbol === symbols[index])
  ) {
    return { status: "unchanged", symbols };
  }
  await store.assignCollectorSubscriptions(symbols);
  return { status: "assigned", symbols };
}

let bootstrap: Promise<void> | null = null;

/**
 * Best-effort once-per-process reconciliation so an application or worker restart
 * cannot leave the collector's shared universe empty or stale. Ongoing reconciliation
 * stays the dedicated scheduled hook's responsibility; this only covers startup and
 * never starts or hosts the collector itself.
 */
export function bootstrapCollectorUniverse(
  env: Record<string, string | undefined> = process.env,
): void {
  if (bootstrap || env["BINANCE_COLLECTOR_ENABLED"] !== "true") return;
  bootstrap = (async () => {
    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
    const { getOperationalStore } = await import("../operational/repository.server");
    await syncCollectorUniverse(supabaseAdmin, getOperationalStore(), env);
  })().catch((error) => {
    console.error(
      `[collector-universe] bootstrap reconciliation failed: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  });
}
