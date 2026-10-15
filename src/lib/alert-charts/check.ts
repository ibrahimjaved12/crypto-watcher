import type { AlertChartsConfig } from "./config";

export type AlertChartCounts = {
  chart_rows: number;
  window_bytes: number;
  png_bytes: number;
  pending_rows: number;
  rendering_rows: number;
  rendered_rows: number;
  failed_rows: number;
  expired_rows: number;
  capture_failures_24h: number;
  capture_failures_total: number;
};

const mb = (bytes: number) => `${(bytes / 1024 ** 2).toFixed(1)} MB`;

/**
 * `mvp:check` line "alert charts": stored size against the caps and the capture failures. FAIL only at a cap
 * (new captures are refused; nothing is deleted automatically). WARN at 80 % of a cap, for any capture failure
 * in the last 24 hours (never FAIL), for failed/expired renders, or for pending work with no ALERT_CHART_DIR;
 * PASS otherwise.
 */
export function classifyAlertCharts(
  counts: AlertChartCounts, config: AlertChartsConfig,
): { level: "PASS" | "WARN" | "FAIL"; detail: string } {
  const bytes = counts.window_bytes + counts.png_bytes;
  const usage = Math.max(counts.chart_rows / config.maxRows, bytes / config.maxBytes);
  const detail = `${counts.chart_rows} charts (rendered ${counts.rendered_rows}, pending ${counts.pending_rows}, `
    + `failed ${counts.failed_rows}, expired ${counts.expired_rows}); windows ${mb(counts.window_bytes)} + `
    + `PNGs ${mb(counts.png_bytes)} of ${mb(config.maxBytes)}, rows cap ${config.maxRows}; `
    + `capture failures ${counts.capture_failures_24h} in 24 h, ${counts.capture_failures_total} total`;
  if (usage >= 1) return { level: "FAIL", detail: `cap reached, new charts are refused: ${detail}` };
  if (!config.captureEnabled) return { level: "WARN", detail: `capture disabled (ALERT_CHARTS_ENABLED=false); ${detail}` };
  if (counts.capture_failures_24h > 0) {
    return { level: "WARN", detail: `capture failures in the last 24 h (windows lost, see alert_chart_capture_failures): ${detail}` };
  }
  if (usage >= 0.8) return { level: "WARN", detail: `above 80% of the cap: ${detail}` };
  if (counts.pending_rows > 0 && !config.directory) {
    return { level: "WARN", detail: `ALERT_CHART_DIR is not set, jobs cannot render: ${detail}` };
  }
  if (counts.failed_rows > 0 || counts.expired_rows > 0) {
    return { level: "WARN", detail: `${detail}; re-run with npm run alert-charts -- --event-id <id>` };
  }
  return { level: "PASS", detail };
}
