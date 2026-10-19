import { supabaseAdmin } from "../integrations/supabase/client.server";
import {
  HOUR_MS,
  createFxStageCall,
  createHostedFxStage,
  runFxHourly,
  type HostedFxStageClient,
} from "../lib/fx/fx-stage.server";
import {
  createFxWalletCall,
  createHostedFxUser,
  runFxUsers,
  type HostedFxUserClient,
} from "../lib/fx/fx-user.server";
import { getOperationalStore } from "../lib/operational/repository.server";
import { pythonServiceConfig } from "../lib/python-service.server";

// Hourly forex market stage runner (#397), a forex child of the worker host (off unless FX_HOURLY_ENABLED=true).
// It runs a few minutes after each completed hour (the forex collector must have stored the boundary minute),
// reads the local operational store and the hosted open setups, asks the Python forward service for the stage
// and commits it with commit_fx_stage. Then the per-user pass (#446): every forex-entitled account consumes the
// committed stages after its cursor through POST /v1/fx/forward/wallet_step. One failing account never stops the
// others. `--once` runs a single pass and exits 1 when a symbol was refused, the stage failed or an account failed.
const OFFSET_MS = 3 * 60_000;

function deps() {
  const store = getOperationalStore();
  if (!store.enabled) throw new Error("The operational database is disabled (OPERATIONAL_DB_ENABLED=true is required)");
  let stage: { url: string; token: string };
  let wallet: { url: string; token: string };
  try {
    stage = pythonServiceConfig("/v1/fx/forward/stage");
    wallet = pythonServiceConfig("/v1/fx/forward/wallet_step");
  } catch {
    throw new Error("The Python forward service is not configured");
  }
  const log = (line: string) => console.log(line);
  const error = (line: string) => console.error(line);
  return {
    stage: {
      now: () => Date.now(),
      store,
      hosted: createHostedFxStage(supabaseAdmin as unknown as HostedFxStageClient),
      callStage: createFxStageCall(stage),
      log,
      error,
    },
    users: {
      now: () => Date.now(),
      store,
      hosted: createHostedFxUser(supabaseAdmin as unknown as HostedFxUserClient),
      callWallet: createFxWalletCall(wallet),
      log,
      error,
    },
  };
}

async function once(): Promise<void> {
  const { stage, users } = deps();
  const result = await runFxHourly(stage);
  // The user pass runs whatever the stage did: older committed stages may still be waiting to be consumed.
  const accounts = await runFxUsers(users, result.boundary);
  const refused = Object.keys(result.failed);
  const problems = [];
  if (result.status === "failed" || refused.length)
    problems.push(`forex stage ${result.status}; refused: ${refused.join(", ") || "none"}`);
  if (accounts.failed) problems.push(`${accounts.failed} of ${accounts.users} forex account(s) failed`);
  if (problems.length) throw new Error(problems.join("; "));
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
