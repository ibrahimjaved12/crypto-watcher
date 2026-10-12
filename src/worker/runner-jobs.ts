import { hostname } from "node:os";
import { supabaseAdmin } from "../integrations/supabase/client.server";
import type { Json, Tables } from "../integrations/supabase/types";
import { forwardDeps, forwardTrendDeps, FORWARD_STRATEGY_IDS } from "../lib/forward/forward-deps.server";
import { runForward } from "../lib/forward/forward-run.server";
import { runForwardTrend } from "../lib/forward/forward-trend-run.server";

if (process.env["RUNNER_JOBS_ENABLED"] !== "true") {
  console.log("Runner jobs are disabled.");
  process.exit(0);
}

const workerId = `${hostname()}-${process.pid}`;
const LEASE_SECONDS = 120;
const POLL_MS = 2_000;
const CLAIM_BACKOFF_MS = 10_000;
const RENEW_MS = 30_000;
const SHUTDOWN_MS = 60_000;
let stopping = false;
let shutdownDeadline: ReturnType<typeof setTimeout> | undefined;
let wake: (() => void) | undefined;

/** Never store a stack or configuration value in a user-visible error or machine log. */
function safeError(error: unknown): string {
  const name = error instanceof Error ? error.name : "Error";
  const message = error instanceof Error ? error.message
    : error && typeof error === "object" && "message" in error && typeof error.message === "string"
      ? error.message : "Runner job failed";
  let safe = `${name}: ${message}`;
  const values = Object.values(process.env).filter((value): value is string => !!value)
    .sort((a, b) => b.length - a.length);
  for (const value of values) {
    safe = safe.replaceAll(value, "[redacted]");
    safe = safe.replaceAll(JSON.stringify(value).slice(1, -1), "[redacted]");
  }
  return safe.replace(/\s+/g, " ").slice(0, 500);
}

async function sleep(ms: number) {
  if (stopping) return;
  await new Promise<void>((resolve) => {
    const timer = setTimeout(() => { wake = undefined; resolve(); }, ms);
    wake = () => { clearTimeout(timer); wake = undefined; resolve(); };
  });
}

async function execute(job: Tables<"runner_jobs">) {
  let renewing: Promise<void> | undefined;
  const renewal = setInterval(() => {
    if (renewing) return;
    renewing = (async () => {
      try {
        const { data, error } = await supabaseAdmin.rpc("extend_runner_job_lease", {
          p_id: job.id, p_worker_id: workerId, p_lease_seconds: LEASE_SECONDS,
        });
        if (error) throw error;
        if (!data) {
          clearInterval(renewal);
          console.error("Runner job lease is no longer owned by this worker.");
        }
      } catch (error) {
        console.error(safeError(error));
      }
    })().finally(() => { renewing = undefined; });
  }, RENEW_MS);

  let ok = false;
  let result: Json | null = null;
  let failure: string | null = null;
  try {
    if (job.kind === "forward_signals") {
      result = await runForward(await forwardDeps(), {
        userId: job.user_id, trigger: "on_demand", strategyIds: FORWARD_STRATEGY_IDS,
      });
    } else if (job.kind === "forward_trend") {
      result = await runForwardTrend(await forwardTrendDeps(), {
        userId: job.user_id, trigger: "on_demand",
      });
    } else {
      throw new Error("Unsupported runner job kind");
    }
    ok = true;
  } catch (error) {
    failure = safeError(error);
    console.error(failure);
  }
  try {
    // Keep renewing until the completion write settles; a stalled write must not lose its lease.
    const { data, error } = await supabaseAdmin.rpc("finish_runner_job", {
      p_id: job.id, p_worker_id: workerId, p_ok: ok, p_result: result, p_error: failure,
    });
    if (error) throw error;
    if (!data) console.error("Runner job completion was rejected because ownership ended.");
  } catch (error) {
    // Never rerun after an uncertain completion write. The next claimant handles lease expiry.
    console.error(safeError(error));
  } finally {
    clearInterval(renewal);
    await renewing;
  }
}

function shutdown() {
  if (stopping) return;
  stopping = true;
  console.log("Runner jobs stopping; allowing active work up to 60 seconds to finish.");
  wake?.();
  shutdownDeadline = setTimeout(() => {
    console.log("Runner jobs shutdown deadline reached; unfinished work is left to lease expiry.");
    process.exit(0);
  }, SHUTDOWN_MS);
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
process.on("SIGHUP", shutdown);

console.log(`Runner jobs ready (${workerId}).`);
while (!stopping) {
  try {
    const { data, error } = await supabaseAdmin.rpc("claim_runner_job", {
      p_worker_id: workerId, p_lease_seconds: LEASE_SECONDS,
    });
    if (error) throw error;
    const job = data?.[0];
    if (job && !stopping) await execute(job); // exactly one job at a time
    await sleep(POLL_MS);
  } catch (error) {
    console.error(safeError(error));
    await sleep(CLAIM_BACKOFF_MS);
  }
}
clearTimeout(shutdownDeadline);
process.exit(0);
