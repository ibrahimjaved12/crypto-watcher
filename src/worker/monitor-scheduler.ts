/**
 * Worker-host child `monitor-scheduler` (#30): runs the scheduled monitoring pass (movement alerts and
 * completed-candle TA for every watching user) on the machine, about once a minute. The Worker cron stays
 * off; this child owns scheduled checks.
 *
 *   node scripts/monitor-scheduler.mjs --mode production
 *
 * Each pass starts at the next interval boundary plus a short settle for the 1m candle that just closed,
 * and passes never overlap. The per-user lease inside `runLeasedMonitorForUser` keeps a pass and a manual
 * check from double-running; a "skipped" user is normal and logs nothing.
 */
import { supabaseAdmin } from "../integrations/supabase/client.server";
import { getOperationalStore } from "../lib/operational/repository.server";
import { runScheduledMonitorPass } from "../lib/monitor/scheduled-pass.server";
import {
  MONITOR_SCHEDULER_ERROR_BACKOFF_MS,
  monitorSchedulerConfigured,
  monitorSchedulerIntervalMs,
  nextMonitorPassAt,
} from "../lib/worker-host";

if (!monitorSchedulerConfigured(process.env) || !getOperationalStore().enabled) {
  console.log("Monitor scheduler: needs the hosted credentials and the operational database, and MONITOR_SCHEDULER_ENABLED not false; not running.");
  process.exit(0);
}
let intervalMs: number;
try {
  intervalMs = monitorSchedulerIntervalMs(process.env);
} catch (error) {
  console.error(`Monitor scheduler: ${error instanceof Error ? error.message : "invalid interval"}`);
  process.exit(1);
}

function message(error: unknown) {
  return (error instanceof Error ? error.message : String(error)).replace(/\s+/g, " ").slice(0, 300);
}

let stopping = false;
let wake: (() => void) | undefined;
const sleep = (ms: number) => new Promise<void>((done) => {
  const timer = setTimeout(() => { wake = undefined; done(); }, Math.max(0, ms));
  wake = () => { clearTimeout(timer); wake = undefined; done(); };
});
// Finish the current pass, then exit: a signal only wakes a sleeping loop, never interrupts a pass.
for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"] as const) {
  process.on(signal, () => { stopping = true; wake?.(); });
}

console.log(`Monitor scheduler ready; a pass about every ${intervalMs / 1000} s.`);
let target: number | null = null;
while (!stopping) {
  target = nextMonitorPassAt(Date.now(), target, intervalMs);
  await sleep(target - Date.now());
  if (stopping) break;
  try {
    const results = await runScheduledMonitorPass(supabaseAdmin);
    const failed = results.filter((r) => r.status === "failed" || r.status === "partial");
    const alerts = results.reduce((sum, r) => sum + r.alertsCreated, 0);
    const taSaved = results.reduce((sum, r) => sum + r.metrics.taSignalsSaved, 0);
    // One line only when something happened worth reading; a healthy quiet pass logs nothing.
    if (failed.length) {
      const detail = failed.map((r) => message(r.error ?? r.status)).slice(0, 3).join(" | ");
      console.error(`Monitor pass: ${failed.length} of ${results.length} users ${failed.length === 1 ? "was" : "were"} not fully checked: ${detail}`);
    }
    if (alerts || taSaved) console.log(`Monitor pass: ${alerts} alert(s), ${taSaved} TA signal(s) saved for ${results.length} user(s).`);
  } catch (error) {
    console.error(`Monitor scheduler: ${message(error)}; retrying in ${MONITOR_SCHEDULER_ERROR_BACKOFF_MS / 1000}s.`);
    await sleep(MONITOR_SCHEDULER_ERROR_BACKOFF_MS);
  }
}
console.log("Monitor scheduler stopped.");
process.exit(0);
