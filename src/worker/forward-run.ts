import type { SupabaseClient } from "@supabase/supabase-js";

import { supabaseAdmin } from "../integrations/supabase/client.server";
import { forwardDeps, FORWARD_STRATEGY_IDS } from "../lib/forward/forward-deps.server";
import { backfillOnly, runHourlyForAccounts } from "../lib/forward/forward-job.server";
import { HOUR_MS } from "../lib/forward/forward-run.server";
import { listForwardUsers } from "../lib/forward/forward-users.server";

// Local forward-test job (#239 P11, P21): runs once per completed hour (two minutes after the hour,
// so the collector has persisted the last 1m candle) for every registered account; FORWARD_USER_ID
// optionally narrows it to one. `--once` runs a single pass and exits (1 if an account failed).
// `--backfill-only` seeds the 1m history for the six symbols (REST klines, progress logged) and exits.
// Production scheduling is not done (docs/production-todo.md).
async function once() {
  const { failed } = await runHourlyForAccounts({
    listUsers: () => listForwardUsers(supabaseAdmin as unknown as SupabaseClient, process.env["FORWARD_USER_ID"]),
    deps: () => forwardDeps(),
    strategyIds: FORWARD_STRATEGY_IDS,
    log: (line) => console.log(line),
    error: (line) => console.error(line),
  });
  if (failed) throw new Error(`${failed} account run(s) failed; the other accounts were processed`);
}

async function seedHistory() {
  const reports = await backfillOnly(await forwardDeps(), (line) => console.log(line));
  const failed = reports.filter((report) => report.error);
  console.log(
    `[forward-backfill] done: ${reports.map((report) => `${report.symbol} ${report.days}d/${report.missing} missing`).join(", ")}`,
  );
  if (failed.length) throw new Error(`backfill failed for ${failed.map((report) => report.symbol).join(", ")}; rerun to resume`);
}

async function loop() {
  for (;;) {
    try {
      await once();
    } catch (error) {
      console.error(`[forward-run] ${error instanceof Error ? error.message : String(error)}`);
    }
    const now = Date.now();
    const next = Math.floor(now / HOUR_MS) * HOUR_MS + HOUR_MS + 120_000;
    await new Promise((resolve) => setTimeout(resolve, next - now));
  }
}

if (process.argv.includes("--backfill-only")) {
  await seedHistory().catch((error) => {
    console.error(`[forward-run] ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  });
} else if (process.argv.includes("--once")) {
  await once().catch((error) => {
    console.error(`[forward-run] ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  });
} else {
  await loop();
}
