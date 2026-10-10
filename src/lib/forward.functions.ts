import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import { z } from "zod";
import {
  exportForwardLog,
  loadForwardDashboard,
  loadForwardLogPage,
  loadForwardReport,
} from "./forward/forward-dashboard";
import { forwardFiltersSchema } from "./forward/forward-filters";
import { loadTrendDashboard } from "./forward/forward-trend-dashboard";

/** On-demand forward run for the signed-in account (#239 P11); the hourly job uses the same path. */
export const runForwardNow = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => {
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
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => loadForwardDashboard(context.supabase as never));

const logInput = z.object({
  kind: z.enum(["signals", "outcomes", "ledger"]),
  cursor: z.string().max(100).nullable().default(null),
  filters: forwardFiltersSchema,
});
const exportInput = z.object({ kind: z.enum(["signals", "outcomes", "ledger"]), filters: forwardFiltersSchema });

/** Results by strategy (n, mean, SE, matched random-timing control, non-trades) for the filters. */
export const getForwardReport = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .validator((input: unknown) => forwardFiltersSchema.parse(input))
  .handler(async ({ context, data }) => loadForwardReport(context.supabase as never, data));

/** One page (50 rows) of the signal, outcome or ledger log; `cursor` comes from the previous page. */
export const getForwardLog = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .validator((input: unknown) => logInput.parse(input))
  .handler(async ({ context, data }) =>
    loadForwardLogPage(context.supabase as never, data.kind, data.filters, data.cursor));

/** CSV text of up to 50,000 log rows honouring the filters. */
export const exportForwardLogCsv = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .validator((input: unknown) => exportInput.parse(input))
  .handler(async ({ context, data }) => exportForwardLog(context.supabase as never, data.kind, data.filters));

/** On-demand daily trend track run for the signed-in account (#239 P14); the daily job uses the same path. */
export const runForwardTrendNow = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => {
    const { forwardTrendDeps } = await import("./forward/forward-deps.server");
    const { runForwardTrend } = await import("./forward/forward-trend-run.server");
    return runForwardTrend(await forwardTrendDeps(), { userId: context.userId, trigger: "on_demand" });
  });

/** Trend track dashboard data, read with the user's own client (RLS: own rows only). */
export const getTrendDashboard = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => loadTrendDashboard(context.supabase as never));
