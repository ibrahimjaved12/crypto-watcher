import { createClient, type SupabaseClient } from "@supabase/supabase-js";

import { operationalDbConfig } from "../operational/config.server";
import { createOperationalFetch } from "../operational/repository.server";
import type { AlertChartsConfig } from "./config";
import type { ChartCapture } from "./capture";

/** One operational alert_charts row as the consumer sees it. */
export type AlertChartJob = {
  chart_id: string;
  event_kind: "movement_alert" | "pattern";
  event_id: string;
  symbol: string;
  timeframe_minutes: number;
  event_candle_at: string;
  candle_window: Record<string, unknown>;
  attempts: number;
};

export type AlertChartDiagnostics = {
  chart_rows: number;
  window_bytes: number;
  png_bytes: number;
  pending_rows: number;
  rendering_rows: number;
  rendered_rows: number;
  failed_rows: number;
  expired_rows: number;
  oldest_pending_at: string | null;
  newest_created_at: string | null;
};

export interface AlertChartStore {
  enqueue(capture: ChartCapture): Promise<{ chartId: string; created: boolean }>;
  claim(workerId: string, chartId?: string): Promise<AlertChartJob | null>;
  finish(chartId: string, workerId: string, result:
    | { ok: true; path: string; sha256: string; bytes: number }
    | { ok: false; error: string }): Promise<boolean>;
  requeue(eventId: string): Promise<string[]>;
  diagnostics(): Promise<AlertChartDiagnostics>;
}

type RpcClient = Pick<SupabaseClient, "rpc">;

export function createAlertChartStore(client: RpcClient, config: AlertChartsConfig): AlertChartStore {
  const call = async <T>(name: string, args: Record<string, unknown> = {}): Promise<T> => {
    const { data, error } = await client.rpc(name, args);
    if (error) throw new Error(`${name}: ${error.message}`);
    return data as T;
  };
  return {
    async enqueue(capture) {
      const rows = await call<Array<{ chart_id: string; created: boolean }>>("enqueue_alert_chart", {
        p_event_kind: capture.eventKind,
        p_event_id: capture.eventId,
        p_user_id: capture.userId,
        p_symbol: capture.symbol,
        p_timeframe_minutes: capture.timeframeMinutes,
        p_event_candle_at: capture.eventCandleAt,
        p_rule_version: capture.ruleVersion,
        p_scope_key: capture.scopeKey,
        p_window_version: capture.windowVersion,
        p_window: capture.window,
        p_max_rows: config.maxRows,
        p_max_bytes: config.maxBytes,
      });
      const row = rows?.[0];
      if (!row) throw new Error("enqueue_alert_chart returned no row");
      return { chartId: row.chart_id, created: row.created };
    },
    async claim(workerId, chartId) {
      const rows = await call<AlertChartJob[]>("claim_alert_chart", {
        p_worker_id: workerId,
        p_lease_seconds: config.leaseSeconds,
        p_expiry_minutes: config.pendingExpiryMinutes,
        p_chart_id: chartId ?? null,
      });
      return rows?.[0] ?? null;
    },
    async finish(chartId, workerId, result) {
      return call<boolean>("finish_alert_chart", {
        p_chart_id: chartId,
        p_worker_id: workerId,
        p_ok: result.ok,
        p_png_path: result.ok ? result.path : null,
        p_png_sha256: result.ok ? result.sha256 : null,
        p_png_bytes: result.ok ? result.bytes : null,
        p_error: result.ok ? null : result.error,
      });
    },
    async requeue(eventId) {
      const rows = await call<Array<string | { requeue_alert_chart: string }>>("requeue_alert_chart", {
        p_event_id: eventId,
      });
      return (rows ?? []).map((row) => (typeof row === "string" ? row : row.requeue_alert_chart));
    },
    async diagnostics() {
      const rows = await call<AlertChartDiagnostics[]>("get_alert_chart_diagnostics");
      const row = rows?.[0];
      if (!row) throw new Error("get_alert_chart_diagnostics returned no row");
      return {
        ...row,
        chart_rows: Number(row.chart_rows), window_bytes: Number(row.window_bytes),
        png_bytes: Number(row.png_bytes), pending_rows: Number(row.pending_rows),
        rendering_rows: Number(row.rendering_rows), rendered_rows: Number(row.rendered_rows),
        failed_rows: Number(row.failed_rows), expired_rows: Number(row.expired_rows),
      };
    },
  };
}

let singleton: AlertChartStore | null | undefined;

/** The operational alert chart store, or null when the operational database is disabled. */
export function getAlertChartStore(config: AlertChartsConfig): AlertChartStore | null {
  if (singleton !== undefined) return singleton;
  const operational = operationalDbConfig();
  if (!operational.enabled) return (singleton = null);
  const client = createClient(operational.url, operational.serviceRoleKey, {
    global: { fetch: createOperationalFetch(operational.serviceRoleKey) },
    auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false },
  });
  return (singleton = createAlertChartStore(client, config));
}
