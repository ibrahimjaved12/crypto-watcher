import { forwardTrendDeps } from "../lib/forward/forward-deps.server";
import { msUntilNextDailyRun, runForwardTrend } from "../lib/forward/forward-trend-run.server";

// Local daily trend track job (#239 P14): runs once per day at 00:05 UTC (the previous UTC day's
// kline is complete by then) for the account FORWARD_USER_ID. `--once` runs a single evaluation and
// exits. Re-runs are no-ops per (track, day). Production scheduling: docs/production-todo.md.
const userId = process.env["FORWARD_USER_ID"] ?? "";
if (!/^[0-9a-f-]{36}$/i.test(userId)) {
  console.error("[forward-trend] FORWARD_USER_ID must be the account UUID that owns the track");
  process.exit(1);
}

async function once() {
  const summary = await runForwardTrend(await forwardTrendDeps(), { userId, trigger: "daily" });
  console.log(`[forward-trend] ${summary.runKey} ${summary.status}${summary.reason ? `: ${summary.reason}` : ""}` +
    (summary.counts ? ` ${JSON.stringify(summary.counts)}` : ""));
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
