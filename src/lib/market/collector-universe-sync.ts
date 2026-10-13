import type { CollectorUniverseSync } from "./collector-subscriptions.server";
import { collectorLogMessage } from "./collector-logging";

export const DEFAULT_UNIVERSE_SYNC_INTERVAL_MS = 60_000;
export const MIN_UNIVERSE_SYNC_INTERVAL_MS = 10_000;
const MAX_TIMER_MS = 2_147_483_647;
type Env = Record<string, string | undefined>;

/** Invalid/empty settings use 60s; positive integers below 10s are clamped. */
export function collectorUniverseSyncInterval(env: Env): number {
  const raw = env["COLLECTOR_UNIVERSE_SYNC_INTERVAL_MS"]?.trim();
  if (!raw || !/^\d+$/.test(raw)) return DEFAULT_UNIVERSE_SYNC_INTERVAL_MS;
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0 || value > MAX_TIMER_MS) {
    return DEFAULT_UNIVERSE_SYNC_INTERVAL_MS;
  }
  return Math.max(MIN_UNIVERSE_SYNC_INTERVAL_MS, value);
}

/** Shared by the supervisor and standalone entrypoint; never prints env values. */
export function collectorUniverseSyncConfigured(env: Env): boolean {
  return (
    env["COLLECTOR_UNIVERSE_SYNC_ENABLED"] !== "false" &&
    env["BINANCE_COLLECTOR_ENABLED"] === "true" &&
    env["OPERATIONAL_DB_ENABLED"] === "true" &&
    [
      "SUPABASE_URL",
      "SUPABASE_SERVICE_ROLE_KEY",
      "OPERATIONAL_SUPABASE_URL",
      "OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY",
    ].every((name) => Boolean(env[name]?.trim()))
  );
}

/**
 * Pure scheduling boundary: the entrypoint supplies its statically imported sync
 * function and clients before Vite runnerImport closes. No lazy imports on ticks.
 * One pass at a time, initially immediate, then an interval after each completion.
 */
export function startCollectorUniverseSync(options: {
  sync(): Promise<CollectorUniverseSync>;
  env?: Env;
  now?: () => number;
  log?: (line: string) => void;
}): { stop(): void } {
  const interval = collectorUniverseSyncInterval(options.env ?? process.env);
  const log = options.log ?? ((line: string) => console.info(line));
  const now = options.now ?? Date.now;
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let symbols: number | null = null;
  let newestAssignmentAt: string | null = null;

  const schedule = (delay: number): void => {
    if (stopped) return;
    // Keep referenced: this child must survive between passes (#326).
    timer = setTimeout(tick, delay);
  };
  const tick = async (): Promise<void> => {
    timer = null;
    if (stopped) return;
    let status: CollectorUniverseSync["status"] = "skipped";
    let detail = "";
    try {
      const result = await options.sync();
      status = result.status;
      if (result.status === "skipped") {
        detail = ` reason=${collectorLogMessage(result.reason).slice(0, 200)}`;
      } else {
        symbols = result.symbols.length;
        if (result.status === "assigned") newestAssignmentAt = new Date(now()).toISOString();
      }
    } catch (error) {
      // Hosted reads and a starting operational DB must not end this process.
      detail = ` error=${collectorLogMessage(error).slice(0, 200)} retry=next_interval`;
    }
    log(
      `[collector-universe-sync] heartbeat status=${status} symbols=${symbols ?? "unknown"} newest_assignment_at=${newestAssignmentAt ?? "never"}${detail}`,
    );
    schedule(interval);
  };

  schedule(0);
  return {
    stop() {
      stopped = true;
      if (timer !== null) clearTimeout(timer);
      timer = null;
    },
  };
}
