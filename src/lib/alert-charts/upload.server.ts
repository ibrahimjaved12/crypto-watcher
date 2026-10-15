/**
 * Machine side of the hosted chart copy (#298): upload one rendered movement-alert PNG to the owner's private
 * hosted bucket through the site's hook, report progress, and classify failures. The local PNG is only read,
 * never moved or deleted, so a failed or offline upload loses nothing; transient failures are retried by the
 * consumer with backoff (alert_charts.upload_next_at). No hosted service key is involved: the machine holds the
 * shared RUNNER_GATEWAY_SECRET and a one-time signed upload URL.
 */
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

import { HOOK_PATH } from "./hosted";
import type { AlertChartUploadJob } from "./store.server";

export type UploadDeps = {
  hosted: { origin: string; secret: string };
  fetch: typeof fetch;
  readFile: (path: string) => Promise<Uint8Array>;
  timeoutMs?: number;
};

export type UploadResult = { ok: true } | { ok: false; permanent: boolean; error: string };

export const defaultUploadDeps = (hosted: { origin: string; secret: string }): UploadDeps =>
  ({ hosted, fetch: (...args) => fetch(...args), readFile });

async function hook(deps: UploadDeps, body: Record<string, unknown>) {
  const response = await deps.fetch(`${deps.hosted.origin}${HOOK_PATH}`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-runner-secret": deps.hosted.secret },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(deps.timeoutMs ?? 15_000),
  });
  let json: Record<string, unknown> = {};
  try { json = (await response.json()) as Record<string, unknown>; } catch { /* non-JSON error page */ }
  return { status: response.status, json };
}

const text = (value: unknown, fallback: string) =>
  (typeof value === "string" && value ? value : fallback).replace(/\s+/g, " ").slice(0, 200);

/** Best effort: progress for the Alerts page; a failure here never matters to the chart. */
export async function reportChartStatus(
  deps: UploadDeps, alertId: string, status: "pending" | "rendering" | "failed", options: { error?: string; renderedLocal?: boolean } = {},
): Promise<void> {
  try {
    await hook(deps, { op: "status", alertId, status, error: options.error ?? null, renderedLocal: options.renderedLocal === true });
  } catch { /* offline: the hosted row keeps its previous state */ }
}

export async function uploadChart(deps: UploadDeps, job: AlertChartUploadJob): Promise<UploadResult> {
  try {
    if (!job.png_path || !job.png_sha256 || !job.png_bytes) return { ok: false, permanent: true, error: "no local PNG recorded" };
    let bytes: Uint8Array;
    try {
      bytes = await deps.readFile(job.png_path);
    } catch {
      return { ok: false, permanent: true, error: "the local PNG file is missing" };
    }
    if (createHash("sha256").update(bytes).digest("hex") !== job.png_sha256) {
      return { ok: false, permanent: true, error: "the local PNG changed after rendering" };
    }
    const begin = await hook(deps, { op: "begin", alertId: job.event_id, bytes: bytes.length });
    if (begin.status === 409 || begin.status === 404 || begin.status === 400) {
      return { ok: false, permanent: true, error: text(begin.json["error"], "refused by the site") };
    }
    if (begin.status !== 200 || begin.json["ok"] !== true) {
      return { ok: false, permanent: false, error: text(begin.json["error"], `site answered ${begin.status}`) };
    }
    const uploadUrl = String(begin.json["uploadUrl"] ?? "");
    if (!/^https:\/\//.test(uploadUrl) && !/^http:\/\/(localhost|127\.0\.0\.1)/.test(uploadUrl)) {
      return { ok: false, permanent: false, error: "the site returned no usable upload URL" };
    }
    const form = new FormData();
    form.append("cacheControl", "3600");
    form.append("", new Blob([Uint8Array.from(bytes)], { type: "image/png" }));
    const put = await deps.fetch(uploadUrl, {
      method: "PUT",
      headers: { "x-upsert": "true", apikey: String(begin.json["apikey"] ?? "") },
      body: form,
      signal: AbortSignal.timeout(deps.timeoutMs ?? 60_000),
    });
    if (!put.ok) return { ok: false, permanent: false, error: `storage answered ${put.status}` };
    const done = await hook(deps, { op: "complete", alertId: job.event_id, bytes: bytes.length, sha256: job.png_sha256 });
    if (done.status === 200 && done.json["ok"] === true) return { ok: true };
    return { ok: false, permanent: false, error: text(done.json["error"], `site answered ${done.status}`) };
  } catch (error) {
    return { ok: false, permanent: false, error: text(error instanceof Error ? error.message : error, "upload failed") };
  }
}
