import { validatePublicSecrets } from "../environment";
import { operationalDbConfig } from "../operational/config.server";
import { pythonServiceConfig } from "../python-service.server";
import { movementFinalizationConfig } from "./movement-finalization";
import { DEFAULT_MARKET_MOVEMENT_CONFIG } from "./movement-metrics-contract";

type Env = Record<string, string | undefined>;

/**
 * Server-only runtime environment contract for the standalone collector worker.
 *
 * The worker ingests public market data and owns only operational working state,
 * so it requires the operational database credentials and nothing from the main
 * Lovable application database. It is a headless backend process with no browser
 * bundle, so it must never depend on build-time `VITE_*` values. Every requirement
 * below is read from the host's runtime `process.env`, which keeps one built
 * artifact host-independent; #19 selects that host. `BINANCE_COLLECTOR_ENABLED` is
 * validated by the collector enablement check that gates this function.
 */
export function validateCollectorWorkerEnvironment(env: Env = process.env): void {
  validatePublicSecrets(env);
  const operational = operationalDbConfig(env);
  if (!operational.enabled) {
    throw new Error("Binance collector requires OPERATIONAL_DB_ENABLED=true");
  }
  const requiredRetentionDays = Math.ceil(
    (DEFAULT_MARKET_MOVEMENT_CONFIG.historicalLookbackMs + 16 * 60_000) /
      (24 * 60 * 60_000),
  );
  if (operational.candleRetentionDays < requiredRetentionDays) {
    throw new Error(
      `Binance collector requires OPERATIONAL_CANDLE_RETENTION_DAYS>=${requiredRetentionDays} for movement normalization history`,
    );
  }
  try {
    pythonServiceConfig("/v1/movement/boundary", env);
  } catch {
    throw new Error(
      "Binance collector requires PYTHON_ANALYSIS_ENABLED=true and a valid Python service URL/token",
    );
  }
  movementFinalizationConfig(env); // MOVEMENT_FINALIZATION_GRACE_MS
}
