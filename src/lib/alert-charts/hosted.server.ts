/**
 * Server side of the hosted chart copy (#298). Three small entry points, all with injected dependencies so tests
 * need no network:
 *
 * - `handleChartHook`: the machine's upload protocol (begin -> signed upload URL, complete, status), reached by
 *   `POST /api/public/hooks/alert-chart-upload`. Image bytes never pass through the Worker: the machine PUTs the
 *   PNG straight to the private bucket with the one-time signed upload URL.
 * - `chartImageFor`: the signed-URL read for the signed-in owner of an alert.
 * - `deleteAlertAndChart`: removes the stored object, then the alert (its metadata row cascades).
 */
import { secretMatches } from "../runner/gateway-core";
import {
  CHART_MAX_BYTES_PER_FILE,
  SIGNED_URL_SECONDS,
  type HostedCaps,
} from "./hosted";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256 = /^[0-9a-f]{64}$/;
export const HOOK_BODY_CAP = 4096;

export type HookInput =
  | { op: "begin"; alertId: string; bytes: number }
  | { op: "complete"; alertId: string; bytes: number; sha256: string }
  | { op: "status"; alertId: string; status: "pending" | "rendering" | "failed"; error: string | null; renderedLocal: boolean };

export type HookReply = { status: number; body: Record<string, unknown> };

export type HostedDeps = {
  rpc(name: string, args: Record<string, unknown>): Promise<{ data: unknown; error: { message: string } | null }>;
  signedUploadUrl(path: string): Promise<{ signedUrl: string; token: string } | null>;
  /** Size in bytes of the stored object, or null when it does not exist. */
  objectSize(path: string): Promise<number | null>;
  publishableKey: string;
  caps: HostedCaps;
};

/** The shared machine secret (the same RUNNER_GATEWAY_SECRET the Worker uses to call the gateway). */
export function hookAuthorized(provided: string | null, env: Record<string, string | undefined> = process.env): boolean {
  const expected = env["RUNNER_GATEWAY_SECRET"] ?? "";
  return env["RUNNER_GATEWAY_ENABLED"] === "true" && /^[A-Za-z0-9_-]{32,256}$/.test(expected)
    && typeof provided === "string" && secretMatches(provided, expected);
}

export function parseHookInput(value: unknown): HookInput | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (typeof v["alertId"] !== "string" || !UUID.test(v["alertId"])) return null;
  const alertId = v["alertId"].toLowerCase();
  const bytes = v["bytes"];
  if (v["op"] === "begin") {
    return Number.isInteger(bytes) && (bytes as number) >= 1 && (bytes as number) <= CHART_MAX_BYTES_PER_FILE
      ? { op: "begin", alertId, bytes: bytes as number } : null;
  }
  if (v["op"] === "complete") {
    return Number.isInteger(bytes) && (bytes as number) >= 1 && (bytes as number) <= CHART_MAX_BYTES_PER_FILE
      && typeof v["sha256"] === "string" && SHA256.test(v["sha256"])
      ? { op: "complete", alertId, bytes: bytes as number, sha256: v["sha256"] } : null;
  }
  if (v["op"] === "status") {
    const status = v["status"];
    if (status !== "pending" && status !== "rendering" && status !== "failed") return null;
    const error = typeof v["error"] === "string" ? v["error"].replace(/\s+/g, " ").slice(0, 300) : null;
    return { op: "status", alertId, status, error, renderedLocal: v["renderedLocal"] === true };
  }
  return null;
}

const fail = (status: number, category: string, error: string): HookReply => ({ status, body: { ok: false, category, error } });

export async function handleChartHook(deps: HostedDeps, input: HookInput): Promise<HookReply> {
  try {
    if (input.op === "status") {
      const { error } = await deps.rpc("report_alert_chart_status", {
        p_alert_id: input.alertId, p_status: input.status, p_error: input.error, p_rendered_local: input.renderedLocal,
      });
      if (error) return fail(500, "internal", "Could not record the status.");
      return { status: 200, body: { ok: true } };
    }
    if (input.op === "begin") {
      const { data, error } = await deps.rpc("begin_alert_chart_upload", {
        p_alert_id: input.alertId, p_bytes: input.bytes,
        p_max_charts: deps.caps.maxCharts, p_max_bytes: deps.caps.maxBytes,
      });
      if (error) return fail(500, "internal", "Could not start the upload.");
      const row = (Array.isArray(data) ? data[0] : data) as { ok?: boolean; reason?: string; storage_path?: string } | undefined;
      if (!row?.ok || !row.storage_path) {
        const reason = row?.reason ?? "refused";
        return reason === "cap"
          ? fail(409, "cap", "The per-account chart limit is reached; nothing was deleted.")
          : fail(reason === "alert_gone" ? 404 : 400, reason, "The upload was refused.");
      }
      const signed = await deps.signedUploadUrl(row.storage_path);
      if (!signed) return fail(502, "storage", "Storage did not issue an upload URL.");
      return { status: 200, body: { ok: true, path: row.storage_path, uploadUrl: signed.signedUrl, token: signed.token,
        apikey: deps.publishableKey } };
    }
    // complete: the object must exist with exactly the announced size before the chart is published.
    const { data: begin, error: beginError } = await deps.rpc("begin_alert_chart_upload", {
      p_alert_id: input.alertId, p_bytes: input.bytes,
      p_max_charts: deps.caps.maxCharts, p_max_bytes: deps.caps.maxBytes,
    });
    if (beginError) return fail(500, "internal", "Could not verify the upload.");
    const path = ((Array.isArray(begin) ? begin[0] : begin) as { storage_path?: string } | undefined)?.storage_path;
    if (!path) return fail(409, "refused", "The upload was refused.");
    const size = await deps.objectSize(path);
    if (size !== input.bytes) return fail(422, "size_mismatch", "The stored file is missing or has another size.");
    const { error } = await deps.rpc("complete_alert_chart_upload", {
      p_alert_id: input.alertId, p_sha256: input.sha256, p_bytes: input.bytes,
    });
    if (error) return fail(500, "internal", "Could not publish the chart.");
    return { status: 200, body: { ok: true } };
  } catch {
    return fail(500, "internal", "Request failed.");
  }
}

// ---------------------------------------------------------------- signed read and delete (signed-in owner)

export type ImageDeps = {
  /** The owner's chart row, looked up by alert id (service role); the caller's id is checked here. */
  chartRow(alertId: string): Promise<{ user_id: string; status: string; storage_path: string | null } | null>;
  signedUrls(path: string, filename: string): Promise<{ view: string; download: string } | null>;
};

export type ChartImage =
  | { ok: true; viewUrl: string; downloadUrl: string; expiresInSeconds: number }
  | { ok: false; reason: "not_ready" | "missing_file" };

/** Another user's alert id answers exactly like an unknown id: "Chart not found". */
export async function chartImageFor(deps: ImageDeps, userId: string, alertId: string, symbol = "chart"): Promise<ChartImage> {
  if (!UUID.test(alertId)) throw new Error("Chart not found");
  const row = await deps.chartRow(alertId);
  if (!row || row.user_id !== userId) throw new Error("Chart not found");
  if (row.status !== "ready" || !row.storage_path) return { ok: false, reason: "not_ready" };
  const urls = await deps.signedUrls(row.storage_path, `${symbol}-alert-${alertId.slice(0, 8)}.png`);
  if (!urls) return { ok: false, reason: "missing_file" };
  return { ok: true, viewUrl: urls.view, downloadUrl: urls.download, expiresInSeconds: SIGNED_URL_SECONDS };
}

export type DeleteDeps = {
  chartRow(alertId: string): Promise<{ user_id: string; storage_path: string | null } | null>;
  removeObject(path: string): Promise<void>;
  /** Deletes the alert as the caller (RLS); returns the number of rows deleted. */
  deleteAlert(alertId: string): Promise<number>;
};

/**
 * Remove the hosted chart (storage object, then the alert whose delete cascades to the metadata row). If the
 * object cannot be removed, the alert is kept and the error is raised so the owner can retry; a leftover private
 * object is never silently orphaned.
 */
export async function deleteAlertAndChart(deps: DeleteDeps, userId: string, alertId: string): Promise<{ deleted: boolean; chartRemoved: boolean }> {
  if (!UUID.test(alertId)) throw new Error("Alert not found");
  const row = await deps.chartRow(alertId);
  let chartRemoved = false;
  if (row && row.user_id === userId && row.storage_path) {
    await deps.removeObject(row.storage_path);
    chartRemoved = true;
  }
  const deleted = (await deps.deleteAlert(alertId)) > 0;
  return { deleted, chartRemoved };
}
