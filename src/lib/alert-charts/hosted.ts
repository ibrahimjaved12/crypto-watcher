/**
 * Hosted copy of movement-alert charts (#298): constants, the Worker-side caps and the pure view model
 * (alert row + chart metadata -> what the Alerts page shows). No I/O.
 */
import { MOVEMENT_RULE_VERSION } from "./capture";

export const CHART_BUCKET = "alert-charts";
export const CHART_MAX_BYTES_PER_FILE = 2_097_152;
export const HOOK_PATH = "/api/public/hooks/alert-chart-upload";
/** Signed URLs live ten minutes; the page asks again after five. */
export const SIGNED_URL_SECONDS = 600;
/**
 * A chart still waiting for the machine after this long is shown as expired. It matches the machine's default
 * ALERT_CHART_PENDING_EXPIRY_MINUTES (7 days); the Worker does not read the machine's setting.
 */
export const WAITING_EXPIRES_AFTER_MS = 7 * 24 * 60 * 60 * 1000;

export type HostedCaps = { maxCharts: number; maxBytes: number };

/** Per-account caps on stored charts; refused visibly at the cap, never deleting. Worker env, server only. */
export function hostedCaps(env: Record<string, string | undefined> = process.env): HostedCaps {
  const read = (name: string, fallback: number, min: number, max: number) => {
    const raw = env[name]?.trim();
    const value = raw ? Number(raw) : fallback;
    if (!Number.isSafeInteger(value) || value < min || value > max) throw new Error(`${name} must be ${min}–${max}`);
    return value;
  };
  return {
    maxCharts: read("ALERT_CHART_HOSTED_MAX_CHARTS", 5000, 1, 1_000_000),
    maxBytes: read("ALERT_CHART_HOSTED_MAX_BYTES", 1024 ** 3, CHART_MAX_BYTES_PER_FILE, 1024 ** 4),
  };
}

export type AlertForChart = {
  id: string;
  symbol: string;
  triggered_at: string;
  is_test: boolean;
  comparison_mode: string;
  change_pct: number;
  threshold_pct: number;
  price: number | null;
  baseline_price: number | null;
  baseline_at: string | null;
  observed_at: string | null;
  rule: string;
  data_source: string;
};

export type ChartMeta = {
  alert_id: string;
  status: "pending" | "rendering" | "ready" | "failed" | "capped";
  rendered_local: boolean;
  error: string | null;
  bytes: number | null;
  rendered_at: string | null;
  created_at: string;
  updated_at: string;
};

export type ChartState =
  | "none" | "waiting" | "rendering" | "not_uploaded" | "ready" | "failed" | "expired" | "capped";

export type ChartViewModel = {
  state: ChartState;
  /** Short heading of the panel state. */
  title: string;
  /** One plain sentence. */
  body: string;
  /** The row's chevron shows a dot. */
  ready: boolean;
  /** The signed URL is worth requesting. */
  loadImage: boolean;
  /** The page should refresh the metadata while the state can still change. */
  changing: boolean;
};

function age(from: string, now: number): string {
  const minutes = Math.max(0, Math.round((now - Date.parse(from)) / 60_000));
  if (minutes < 1) return "less than a minute";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.round(hours / 24)} days`;
}

const view = (
  state: ChartState, title: string, body: string, flags: Partial<Pick<ChartViewModel, "ready" | "loadImage" | "changing">> = {},
): ChartViewModel => ({ state, title, body, ready: false, loadImage: false, changing: false, ...flags });

/** Every state the Alerts page can show for one alert; never a blank box. */
export function chartViewModel(alert: Pick<AlertForChart, "is_test">, meta: ChartMeta | undefined, now: number): ChartViewModel {
  if (alert.is_test) return view("none", "No chart for test alerts", "A test alert is not a market event, so no chart is made for it.");
  if (!meta) {
    return view("none", "No chart for this alert",
      "This alert is older than the chart feature, or chart capture was off on your machine when it fired.");
  }
  const waited = now - Date.parse(meta.created_at);
  switch (meta.status) {
    case "ready":
      return view("ready", "Setup chart", "The chart as it looked when the alert fired.", { ready: true, loadImage: true });
    case "capped":
      return view("capped", "Chart not stored",
        meta.error ?? "Your stored chart limit is reached. Nothing was deleted; the chart stays on your machine.");
    case "failed":
      return view("failed", "Chart could not be made", `The machine reported: ${(meta.error ?? "render failed").slice(0, 160)}`);
    case "rendering":
      return view("rendering", "Drawing the chart…", `Your machine is drawing it now (started ${age(meta.updated_at, now)} ago).`,
        { changing: true });
    case "pending":
      if (waited > WAITING_EXPIRES_AFTER_MS) {
        return view("expired", "Chart expired",
          "The machine did not draw this chart within 7 days, so it was dropped. It can be re-run on the machine with its alert id.");
      }
      if (meta.rendered_local) {
        return view("not_uploaded", "Chart not uploaded yet",
          `Drawn on your machine ${age(meta.updated_at, now)} ago; the upload is waiting and retries on its own.`, { changing: true });
      }
      return view("waiting", "Waiting for your machine",
        `The alert fired ${age(meta.created_at, now)} ago and the machine has not drawn the chart yet. It draws it when it is online.`,
        { changing: true });
  }
}

export type AlertFacts = {
  firedAtUtc: string;
  firedAtIso: string;
  baselinePrice: number | null;
  baselineAtUtc: string | null;
  baselineAtIso: string | null;
  alertPrice: number | null;
  changePct: number;
  thresholdPct: number;
  rule: string;
  ruleVersion: string;
  dataSource: string;
};

const utc = (iso: string | null) => {
  if (!iso || !Number.isFinite(Date.parse(iso))) return null;
  return `${new Date(iso).toISOString().slice(0, 16).replace("T", " ")} UTC`;
};

/** The facts the chart does not show; the page formats the ISO values as local time. */
export function alertFacts(alert: AlertForChart): AlertFacts {
  const fired = alert.observed_at ?? alert.triggered_at;
  return {
    firedAtUtc: utc(fired) ?? "unknown",
    firedAtIso: fired,
    baselinePrice: alert.baseline_price === null ? null : Number(alert.baseline_price),
    baselineAtUtc: utc(alert.baseline_at),
    baselineAtIso: alert.baseline_at,
    alertPrice: alert.price === null ? null : Number(alert.price),
    changePct: Number(alert.change_pct),
    thresholdPct: Number(alert.threshold_pct),
    rule: alert.rule,
    ruleVersion: MOVEMENT_RULE_VERSION,
    dataSource: alert.data_source,
  };
}
