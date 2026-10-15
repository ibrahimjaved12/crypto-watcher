/**
 * Machine-side render step for one claimed alert chart job (#299): run the Python renderer on the
 * stored window, write the PNG under ALERT_CHART_DIR, record its path, sha256 and size. Rendering
 * runs in a child process with a timeout; its stderr line becomes the job error.
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, readFile } from "node:fs/promises";
import { join, resolve } from "node:path";

import type { AlertChartJob, AlertChartStore } from "./store.server";

export type RenderRunner = (job: { window: Record<string, unknown>; path: string }) => Promise<void>;

/** `<dir>/<kind>/<SYMBOL>/<YYYY-MM-DD of the event candle>/<chart_id>.png` */
export function chartPath(directory: string, job: Pick<AlertChartJob, "chart_id" | "event_kind" | "symbol" | "event_candle_at">): string {
  if (!/^[0-9a-f-]{36}$/.test(job.chart_id) || !/^[A-Z0-9]{2,30}$/.test(job.symbol)) {
    throw new Error("Unsafe chart id or symbol for a file path");
  }
  const day = new Date(job.event_candle_at).toISOString().slice(0, 10);
  return join(resolve(directory), job.event_kind, job.symbol, day, `${job.chart_id}.png`);
}

export function pythonRenderer(options: { python: string; cwd: string; timeoutMs: number }): RenderRunner {
  return (job) => new Promise<void>((done, fail) => {
    const child = spawn(options.python, ["-m", "market_analysis_tools.alert_charts", "render"], {
      cwd: options.cwd, stdio: ["pipe", "pipe", "pipe"], env: { ...process.env, MPLBACKEND: "Agg" },
    });
    let stderr = "";
    child.stderr.on("data", (chunk: Buffer) => { stderr = (stderr + chunk.toString()).slice(-2000); });
    child.stdout.resume();
    const timer = setTimeout(() => child.kill("SIGKILL"), options.timeoutMs);
    child.on("error", (error) => { clearTimeout(timer); fail(error); });
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      if (code === 0) return done();
      const last = stderr.trim().split("\n").at(-1) || (signal ? `renderer killed (${signal})` : `renderer exited ${code}`);
      fail(new Error(last));
    });
    child.stdin.end(JSON.stringify(job));
  });
}

function safe(error: unknown): string {
  return (error instanceof Error ? error.message : String(error)).replace(/\s+/g, " ").slice(0, 500);
}

/** Render one claimed job and record the outcome. Returns the final status written (and the error when failed). */
export async function renderClaimedJob(
  job: AlertChartJob,
  deps: { store: AlertChartStore; workerId: string; directory: string; render: RenderRunner },
): Promise<{ status: "rendered" | "failed"; error?: string }> {
  let result: Parameters<AlertChartStore["finish"]>[2];
  try {
    const path = chartPath(deps.directory, job);
    await mkdir(resolve(path, ".."), { recursive: true });
    await deps.render({ window: job.candle_window, path });
    const data = await readFile(path);
    if (data.length === 0) throw new Error("Renderer wrote an empty file");
    result = { ok: true, path, sha256: createHash("sha256").update(data).digest("hex"), bytes: data.length };
  } catch (error) {
    result = { ok: false, error: safe(error) };
  }
  const accepted = await deps.store.finish(job.chart_id, deps.workerId, result);
  if (!accepted) throw new Error("Alert chart completion rejected: the lease is no longer owned");
  return result.ok ? { status: "rendered" } : { status: "failed", error: result.error };
}
