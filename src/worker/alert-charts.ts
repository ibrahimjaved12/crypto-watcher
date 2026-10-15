/**
 * Worker-host child `alert-charts` (#299): renders pending alert chart jobs from the operational
 * database, one at a time, and stores the PNGs under ALERT_CHART_DIR.
 *
 *   node scripts/alert-charts.mjs --mode production                 # loop (worker host)
 *   node scripts/alert-charts.mjs --mode production --event-id <id> # re-render one event, then exit
 *
 * If the machine is off, jobs wait in the operational database; on the next claim sweep the
 * runner-job rules apply (pending older than ALERT_CHART_PENDING_EXPIRY_MINUTES -> expired, an
 * elapsed lease -> failed). Nothing is replayed automatically; --event-id requeues explicitly.
 */
import { hostname } from "node:os";
import { resolve } from "node:path";

import { alertChartsConfig } from "../lib/alert-charts/config";
import { pythonRenderer, renderClaimedJob } from "../lib/alert-charts/consumer.server";
import { getAlertChartStore, type AlertChartJob, type AlertChartUploadJob } from "../lib/alert-charts/store.server";
import { defaultUploadDeps, reportChartStatus, uploadChart } from "../lib/alert-charts/upload.server";

const POLL_MS = 5_000;
const ERROR_BACKOFF_MS = 30_000;
// The worker host and the npm script both run children from the repository root.
const root = process.cwd();
const config = alertChartsConfig();
const store = getAlertChartStore(config);
if (!store) {
  console.log("Alert charts: the operational database is disabled; nothing to render.");
  process.exit(0);
}
if (!config.directory) {
  console.log("Alert charts: ALERT_CHART_DIR is not set; jobs stay pending until it is.");
  process.exit(0);
}
const directory = config.directory;
const python = process.env["ALERT_CHART_PYTHON"]
  || resolve(root, "python", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const render = pythonRenderer({ python, cwd: resolve(root, "python"), timeoutMs: config.renderTimeoutMs });
const workerId = `${hostname()}-${process.pid}-alert-charts`;

function argValue(name: string): string | null {
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1] ?? null;
}

// Hosted copy of movement-alert charts (#298): only when ALERT_CHART_HOSTED_URL is set. The local PNG is never
// touched by an upload; a failed upload is retried with backoff and the hosted row keeps saying "pending".
const uploadDeps = config.hosted ? defaultUploadDeps(config.hosted) : null;

async function renderAndQueue(job: AlertChartJob): Promise<"rendered" | "failed"> {
  const hosted = uploadDeps !== null && job.event_kind === "movement_alert";
  if (hosted) await reportChartStatus(uploadDeps, job.event_id, "rendering");
  const result = await renderClaimedJob(job, { store: store!, workerId, directory, render });
  if (hosted) {
    if (result.status === "rendered") {
      await store!.queueUpload(job.chart_id);
      await reportChartStatus(uploadDeps, job.event_id, "pending", { renderedLocal: true });
    } else {
      await reportChartStatus(uploadDeps, job.event_id, "failed", { error: result.error ?? "render failed" });
    }
  }
  return result.status;
}

async function attemptUpload(job: AlertChartUploadJob): Promise<boolean> {
  const result = await uploadChart(uploadDeps!, job);
  await store!.finishUpload(job.chart_id, result);
  if (!result.ok) console.error(`Alert chart ${job.chart_id}: upload ${result.permanent ? "refused" : "failed, will retry"}: ${result.error}`);
  return result.ok;
}

function message(error: unknown) {
  return (error instanceof Error ? error.message : String(error)).replace(/\s+/g, " ").slice(0, 300);
}

const eventId = argValue("--event-id");
if (eventId !== null) {
  if (!/^[A-Za-z0-9-]{1,128}$/.test(eventId)) {
    console.error("--event-id must be the alerts.id or ta_signals.id of the event");
    process.exit(2);
  }
  const outcomes = await store.requeue(eventId);
  if (!outcomes.length) {
    console.error("No chart for this event id.");
    process.exit(1);
  }
  let failed = 0;
  for (const item of outcomes.filter((o) => o.outcome === "rendering")) {
    const until = item.leaseExpiresAt ? new Date(item.leaseExpiresAt).toISOString().slice(11, 16) : "?";
    console.error(`Chart ${item.chartId} is rendering right now (lease until ${until} UTC); try again after that.`);
    failed += 1;
  }
  for (const { chartId } of outcomes.filter((o) => o.outcome === "requeued")) {
    const job = await store.claim(workerId, chartId);
    if (!job) { failed += 1; continue; }
    const status = await renderAndQueue(job);
    console.log(`Alert chart ${chartId}: ${status}`);
    if (status !== "rendered") failed += 1;
    else if (uploadDeps && job.event_kind === "movement_alert") {
      const upload = await store.claimUpload(chartId);
      if (upload && !(await attemptUpload(upload))) failed += 1;
    }
  }
  process.exit(failed ? 1 : 0);
}

let stopping = false;
let wake: (() => void) | undefined;
const sleep = (ms: number) => new Promise<void>((done) => {
  const timer = setTimeout(() => { wake = undefined; done(); }, ms);
  wake = () => { clearTimeout(timer); wake = undefined; done(); };
});
for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"] as const) {
  process.on(signal, () => { stopping = true; wake?.(); });
}

console.log(`Alert charts ready (${workerId}); PNGs under ${directory}.`);
while (!stopping) {
  try {
    const job = await store.claim(workerId);
    if (job) {
      const status = await renderAndQueue(job);
      if (status === "failed") console.error(`Alert chart ${job.chart_id} failed; see its error column.`);
      continue;
    }
    const upload = uploadDeps ? await store.claimUpload() : null;
    if (upload) { await attemptUpload(upload); continue; }
    await sleep(POLL_MS);
  } catch (error) {
    console.error(`Alert charts: ${message(error)}; retrying in ${ERROR_BACKOFF_MS / 1000}s.`);
    await sleep(ERROR_BACKOFF_MS);
  }
}
console.log("Alert charts stopped.");
process.exit(0);
