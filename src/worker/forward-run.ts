import { forwardDeps, FORWARD_STRATEGY_IDS } from "../lib/forward/forward-deps.server";
import { HOUR_MS, runForward } from "../lib/forward/forward-run.server";

// Local forward-test job (#239 P11): runs once per completed hour (two minutes after the hour, so
// the collector has persisted the last 1m candle) for the account FORWARD_USER_ID. `--once` runs
// a single evaluation and exits. Production scheduling is not done (docs/production-todo.md).
const userId = process.env["FORWARD_USER_ID"] ?? "";
if (!/^[0-9a-f-]{36}$/i.test(userId)) {
  console.error("[forward-run] FORWARD_USER_ID must be the account UUID that owns the paper run");
  process.exit(1);
}

async function once() {
  const summary = await runForward(await forwardDeps(), {
    userId, trigger: "hourly", strategyIds: FORWARD_STRATEGY_IDS });
  console.log(`[forward-run] ${summary.runKey} ${summary.status}${summary.reason ? `: ${summary.reason}` : ""}` +
    (summary.counts ? ` ${JSON.stringify(summary.counts)}` : ""));
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

if (process.argv.includes("--once")) {
  await once().catch((error) => {
    console.error(`[forward-run] ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  });
} else {
  await loop();
}
