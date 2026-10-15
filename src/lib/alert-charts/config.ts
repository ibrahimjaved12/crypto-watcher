/**
 * Alert chart settings (#299). Server-only; never a VITE_* variable.
 *
 * Capture is on by default wherever the operational database is enabled (in production that is the
 * owner's machine); ALERT_CHARTS_ENABLED=false turns it off. The render consumer starts only when
 * ALERT_CHART_DIR is set.
 */
export type AlertChartsConfig = {
  captureEnabled: boolean;
  /** Absolute directory for PNGs on the machine's disk; null = no consumer (jobs wait, then expire). */
  directory: string | null;
  maxRows: number;
  maxBytes: number;
  /**
   * Pending rows older than this expire. The runner-job rule is 15 minutes; it is intentionally relaxed to
   * 7 days here because the window is stored, so a machine that was offline loses nothing by the wait.
   */
  pendingExpiryMinutes: number;
  leaseSeconds: number;
  renderTimeoutMs: number;
  /**
   * Hosted copy of movement-alert charts (#298): the site origin the machine uploads to (ALERT_CHART_HOSTED_URL)
   * and the shared secret it authenticates with (the existing RUNNER_GATEWAY_SECRET). Both must be set;
   * otherwise nothing is uploaded and no hosted row is created.
   */
  hosted: { origin: string; secret: string } | null;
};

function boundedInteger(value: string | undefined, fallback: number, min: number, max: number, name: string) {
  const parsed = value === undefined || value.trim() === "" ? fallback : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) throw new Error(`${name} must be ${min}–${max}`);
  return parsed;
}

export function alertChartsConfig(env: Record<string, string | undefined> = process.env): AlertChartsConfig {
  const raw = env["ALERT_CHARTS_ENABLED"];
  if (raw !== undefined && raw !== "" && raw !== "true" && raw !== "false") {
    throw new Error("ALERT_CHARTS_ENABLED must be true or false");
  }
  const directory = env["ALERT_CHART_DIR"]?.trim() || null;
  if (directory !== null && !(directory.startsWith("/") || /^[A-Za-z]:[\\/]/.test(directory))) {
    throw new Error("ALERT_CHART_DIR must be an absolute path");
  }
  return {
    captureEnabled: raw !== "false",
    directory,
    maxRows: boundedInteger(env["ALERT_CHART_MAX_ROWS"], 100_000, 100, 10_000_000, "ALERT_CHART_MAX_ROWS"),
    // Windows (2-6 KB) plus PNGs (50-115 KB each); 5 GiB is roughly 50,000 rendered charts.
    maxBytes: boundedInteger(env["ALERT_CHART_MAX_BYTES"], 5 * 1024 ** 3, 1024 ** 2, 1024 ** 4, "ALERT_CHART_MAX_BYTES"),
    pendingExpiryMinutes: boundedInteger(env["ALERT_CHART_PENDING_EXPIRY_MINUTES"], 10_080, 1, 10_080,
      "ALERT_CHART_PENDING_EXPIRY_MINUTES"),
    leaseSeconds: 120,
    renderTimeoutMs: 60_000,
    hosted: hostedConfig(env),
  };
}

function hostedConfig(env: Record<string, string | undefined>): { origin: string; secret: string } | null {
  const raw = env["ALERT_CHART_HOSTED_URL"]?.trim();
  if (!raw) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("ALERT_CHART_HOSTED_URL must be a URL");
  }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if ((url.protocol !== "https:" && !(url.protocol === "http:" && local)) || url.username || url.password
    || url.search || url.hash || url.pathname !== "/") {
    throw new Error("ALERT_CHART_HOSTED_URL must be an https origin without a path");
  }
  const secret = env["RUNNER_GATEWAY_SECRET"] ?? "";
  if (!/^[A-Za-z0-9_-]{32,256}$/.test(secret)) {
    throw new Error("ALERT_CHART_HOSTED_URL needs RUNNER_GATEWAY_SECRET (the same secret the Worker holds)");
  }
  return { origin: url.origin, secret };
}
