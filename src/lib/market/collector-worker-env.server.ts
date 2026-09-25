import { validatePublicSecrets, validateServerSupabase } from "../environment";
import { operationalDbConfig } from "../operational/config.server";
import { movementFinalizationConfig } from "./movement-finalization";

type Env = Record<string, string | undefined>;

/**
 * Server-only runtime environment contract for the standalone collector worker.
 *
 * The worker is a headless backend process with no browser bundle, so it must never
 * depend on build-time `VITE_*` values. Every requirement below is read from the
 * host's runtime `process.env`, which keeps one built artifact host-independent; #19
 * selects that host. `BINANCE_COLLECTOR_ENABLED` is validated by the collector
 * enablement check that gates this function.
 */
export function validateCollectorWorkerEnvironment(env: Env = process.env): void {
  validatePublicSecrets(env);
  validateServerSupabase(env); // APP_PROFILE, main Supabase URL + service-role key
  if (!operationalDbConfig(env).enabled) {
    throw new Error("Binance collector requires OPERATIONAL_DB_ENABLED=true");
  }
  movementFinalizationConfig(env); // MOVEMENT_FINALIZATION_GRACE_MS
}
