import { supabaseAdmin } from "../integrations/supabase/client.server";
import {
  HOUR_MS,
  createFxStageCall,
  createHostedFxStage,
  runFxHourly,
  type HostedFxStageClient,
} from "../lib/fx/fx-stage.server";
import { getOperationalStore } from "../lib/operational/repository.server";
import { pythonServiceConfig } from "../lib/python-service.server";

// Hourly forex market stage runner (#397), a forex child of the worker host (off unless FX_HOURLY_ENABLED=true).
// It runs a few minutes after each completed hour (the forex collector must have stored the boundary minute),
// reads the local operational store and the hosted open setups, asks the Python forward service for the stage
// and commits it with commit_fx_stage. No per-user wallet work. `--once` runs a single pass and exits 1 when a
// symbol was refused or the pass failed.
const OFFSET_MS = 3 * 60_000;

function deps() {
  const store = getOperationalStore();
  if (!store.enabled) throw new Error("The operational database is disabled (OPERATIONAL_DB_ENABLED=true is required)");
  let config: { url: string; token: string };
  try {
    config = pythonServiceConfig("/v1/fx/forward/stage");
  } catch {
    throw new Error("The Python forward service is not configured");
  }
  return {
    now: () => Date.now(),
    store,
    hosted: createHostedFxStage(supabaseAdmin as unknown as HostedFxStageClient),
    callStage: createFxStageCall(config),
    log: (line: string) => console.log(line),
    error: (line: string) => console.error(line),
  };
}

async function once(): Promise<void> {
  const result = await runFxHourly(deps());
  const refused = Object.keys(result.failed);
  if (result.status === "failed" || refused.length)
    throw new Error(`forex stage ${result.status}; refused: ${refused.join(", ") || "none"}`);
}

async function loop(): Promise<never> {
  for (;;) {
    try {
      await once();
    } catch (error) {
      console.error(`[forex-hourly] ${error instanceof Error ? error.message : String(error)}`);
    }
    const now = Date.now();
    const next = Math.floor(now / HOUR_MS) * HOUR_MS + HOUR_MS + OFFSET_MS;
    await new Promise((resolve) => setTimeout(resolve, next - now));
  }
}

try {
  deps(); // fail fast on a misconfigured host before the first hour
  if (process.argv.includes("--once")) await once();
  else await loop();
} catch (error) {
  console.error(`[forex-hourly] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
