import { forwardTrendDeps } from "../lib/forward/forward-deps.server";
import { msUntilNextDailyRun, runForwardTrend } from "../lib/forward/forward-trend-run.server";
import { listForwardUsers } from "../lib/forward/forward-users.server";
import { supabaseAdmin } from "../integrations/supabase/client.server";
import type { SupabaseClient } from "@supabase/supabase-js";

// Local daily trend track job (#239 P14): runs once per day at 00:05 UTC (the previous UTC day's
// kline is complete by then) for all registered accounts. FORWARD_USER_ID optionally narrows it.
// `--once` runs a single evaluation and
// exits. Re-runs are no-ops per (track, day). Production scheduling: docs/production-todo.md.
async function once() {
  const users = await listForwardUsers(
    supabaseAdmin as unknown as SupabaseClient,
    process.env["FORWARD_USER_ID"],
  );
  const deps = await forwardTrendDeps();
  let failed = 0;
  for (const userId of users) {
    try {
      const summary = await runForwardTrend(deps, { userId, trigger: "daily" });
      console.log(
        `[forward-trend] ${userId} ${summary.runKey} ${summary.status}${summary.reason ? `: ${summary.reason}` : ""}` +
          (summary.counts ? ` ${JSON.stringify(summary.counts)}` : ""),
      );
    } catch (error) {
      failed++;
      console.error(
        `[forward-trend] ${userId}: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }
  if (!users.length) console.log("[forward-trend] no registered accounts yet");
  if (failed) throw new Error(`${failed} account runs failed; remaining accounts were processed`);
}

async function loop() {
  for (;;) {
    try {
      await once();
    } catch (error) {
      console.error(`[forward-trend] ${error instanceof Error ? error.message : String(error)}`);
    }
    await new Promise((resolve) => setTimeout(resolve, msUntilNextDailyRun(Date.now())));
  }
}

if (process.argv.includes("--once")) {
  await once().catch((error) => {
    console.error(`[forward-trend] ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  });
} else {
  await loop();
}
