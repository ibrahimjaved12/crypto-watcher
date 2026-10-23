import { createServerFn } from "@tanstack/react-start";
import { assertMarketEnabled, requireMarketAccess } from "@/integrations/supabase/auth-middleware";
import type { SupabaseClient } from "@supabase/supabase-js";
import type { Database } from "@/integrations/supabase/types";
import { z } from "zod";
import {
  exportForwardLog,
  loadForwardDashboard,
  loadForwardLogPage,
  loadForwardReport,
} from "./forward/forward-dashboard";
import { forwardFiltersSchema } from "./forward/forward-filters";
import { loadTrendDashboard } from "./forward/forward-trend-dashboard";

/** Read-only capability; the browser cannot enable machine work. */
export const getMachineCapabilities = createServerFn({ method: "GET" })
  .middleware([requireMarketAccess])
  .handler(async () => {
    const { machineOnlyEnabled } = await import("./machine-only.server");
    return {
      machineOnlyEnabled: machineOnlyEnabled(),
      runnerJobsEnabled: process.env["RUNNER_JOBS_ENABLED"] === "true",
    };
  });

const runNowInput = z.object({ idempotencyKey: z.string().uuid() });

/** The user's own client enforces RLS; service-role access is confined to the host. */
async function enqueueRunnerJob(
  supabase: SupabaseClient<Database>,
  userId: string,
  kind: "forward_signals" | "forward_trend",
  idempotencyKey: string,
) {
  // Stale queued rows would otherwise hold the active-job slot; a failure here is not fatal.
  await supabase.rpc("expire_my_stale_runner_jobs");
  const { data, error } = await supabase.from("runner_jobs")
    .insert({ user_id: userId, kind, idempotency_key: idempotencyKey })
    .select("id, status").single();
  if (!error && data) return { jobId: data.id, status: data.status, alreadyActive: false };
  if (error?.code === "23505") {
    // Check the retry key first: an old completed request must never become a new run.
    const existing = await supabase.from("runner_jobs").select("id, status")
      .eq("user_id", userId).eq("kind", kind).eq("idempotency_key", idempotencyKey).maybeSingle();
    if (existing.error) throw new Error("Could not read the existing runner job. Please try again.");
    if (existing.data)
      return { jobId: existing.data.id, status: existing.data.status, alreadyActive: false };
    const active = await supabase.from("runner_jobs").select("id, status")
      .eq("user_id", userId).eq("kind", kind).in("status", ["queued", "running"]).maybeSingle();
    if (!active.error && active.data)
      return { jobId: active.data.id, status: active.data.status, alreadyActive: true };
  }
  throw new Error("Could not queue the runner job. Please try again.");
}

export const getRunnerJob = createServerFn({ method: "GET" })
  .middleware([requireMarketAccess])
  .validator((input: unknown) => z.object({ jobId: z.string().uuid() }).parse(input))
  .handler(async ({ context, data }) => {
    const job = await context.supabase.from("runner_jobs")
      .select("id, kind, status, created_at, started_at, finished_at, result, error")
      .eq("id", data.jobId).single();
    if (job.error || !job.data) throw new Error("Runner job could not be found.");
    // The row is updated by the next enqueue or the host's next claim; show it as expired now.
    const stale = job.data.status === "queued"
      && Date.now() - new Date(job.data.created_at).getTime() > 15 * 60_000;
    return {
      id: job.data.id,
      kind: job.data.kind,
      status: stale ? "expired" : job.data.status,
      createdAt: job.data.created_at,
      startedAt: job.data.started_at,
      finishedAt: job.data.finished_at,
      result: job.data.result,
      error: job.data.error,
    };
  });

/** On-demand forward run for the signed-in account (#239 P11); the hourly job uses the same path. */
export const runForwardNow = createServerFn({ method: "POST" })
  .middleware([requireMarketAccess])
  .validator((input: unknown) => runNowInput.parse(input))
  .handler(async ({ context, data }) => {
    const { machineOnlyEnabled, requireMachineOnly } = await import("./machine-only.server");
    if (!machineOnlyEnabled() && process.env["RUNNER_JOBS_ENABLED"] === "true")
      return enqueueRunnerJob(context.supabase, context.userId, "forward_signals", data.idempotencyKey);
    requireMachineOnly();
    const { forwardDeps, FORWARD_STRATEGY_IDS } = await import("./forward/forward-deps.server");
    const { runForward } = await import("./forward/forward-run.server");
    return runForward(await forwardDeps(), {
      userId: context.userId,
      trigger: "on_demand",
      strategyIds: FORWARD_STRATEGY_IDS,
    });
  });

/** Dashboard data, read with the user's own client (RLS: own rows only). */
export const getForwardDashboard = createServerFn({ method: "GET" })
  .middleware([requireMarketAccess])
  .handler(async ({ context }) => loadForwardDashboard(context.supabase as never));

const logInput = z.object({
  kind: z.enum(["signals", "outcomes", "ledger"]),
  cursor: z.string().max(100).nullable().default(null),
  filters: forwardFiltersSchema,
});
// The ledger is the only log with a forex twin (the Paper account page's export); the default stays crypto.
const exportInput = z
  .object({
    kind: z.enum(["signals", "outcomes", "ledger"]),
    filters: forwardFiltersSchema,
    market: z.enum(["crypto", "forex"]).default("crypto"),
  })
  .refine((input) => input.market === "crypto" || input.kind === "ledger", { message: "Only the ledger exists for forex" });

/** Results by strategy (n, mean, SE, matched random-timing control, non-trades) for the filters. */
export const getForwardReport = createServerFn({ method: "POST" })
  .middleware([requireMarketAccess])
  .validator((input: unknown) => forwardFiltersSchema.parse(input))
  .handler(async ({ context, data }) => loadForwardReport(context.supabase as never, data));

/** One page (50 rows) of the signal, outcome or ledger log; `cursor` comes from the previous page. */
export const getForwardLog = createServerFn({ method: "POST" })
  .middleware([requireMarketAccess])
  .validator((input: unknown) => logInput.parse(input))
  .handler(async ({ context, data }) =>
    loadForwardLogPage(context.supabase as never, data.kind, data.filters, data.cursor));

/** CSV text of up to 50,000 log rows honouring the filters. */
export const exportForwardLogCsv = createServerFn({ method: "POST" })
  .middleware([requireMarketAccess])
  .validator((input: unknown) => exportInput.parse(input))
  .handler(async ({ context, data }) => {
    assertMarketEnabled(context.markets, data.market);
    return exportForwardLog(context.supabase as never, data.kind, data.filters, data.market);
  });

/** On-demand daily trend track run for the signed-in account (#239 P14); the daily job uses the same path. */
export const runForwardTrendNow = createServerFn({ method: "POST" })
  .middleware([requireMarketAccess])
  .validator((input: unknown) => runNowInput.parse(input))
  .handler(async ({ context, data }) => {
    const { machineOnlyEnabled, requireMachineOnly } = await import("./machine-only.server");
    if (!machineOnlyEnabled() && process.env["RUNNER_JOBS_ENABLED"] === "true")
      return enqueueRunnerJob(context.supabase, context.userId, "forward_trend", data.idempotencyKey);
    requireMachineOnly();
    const { forwardTrendDeps } = await import("./forward/forward-deps.server");
    const { runForwardTrend } = await import("./forward/forward-trend-run.server");
    return runForwardTrend(await forwardTrendDeps(), { userId: context.userId, trigger: "on_demand" });
  });

/** Trend track dashboard data, read with the user's own client (RLS: own rows only). */
export const getTrendDashboard = createServerFn({ method: "GET" })
  .middleware([requireMarketAccess])
  .handler(async ({ context }) => loadTrendDashboard(context.supabase as never));
