/**
 * "Current assessment" view models (#30): pure, client-safe mappers from evidence that already exists (the
 * movement engine's persisted current state, the quote snapshot and the latest saved TA rows) to what the
 * dashboard shows. No engine logic, no thresholds of our own and no prediction: every cut-off used here is
 * either the movement engine's own constant for the saved config version or a plain freshness window named below.
 */
import type { ClassifierMetric, MarketClassificationWindow } from "./market-state-contract";
import {
  MINIMUM_MARKET_UNIVERSE_SIZE,
  MOVEMENT_ENGINE_STALE_AFTER_MS,
  MOVEMENT_POLL_INTERVAL_MS,
  MOVEMENT_PUBLISH_INTERVAL_MS,
  PUBLISHED_COPY_OFFLINE_AFTER_MS,
  type MarketMovementCurrentState,
  type MovementEngineStatus,
} from "./market-movement-state";
import { DEFAULT_MARKET_MOVEMENT_CONFIG, type Metric } from "./movement-metrics-contract";
import { QUOTE_STALE_MS } from "./quote-calc";
import { humanizeToken } from "../labels";

export const ASSESSMENT_WINDOWS = [1, 5, 15] as const;
export const TA_ASSESSMENT_FRAMES = [15, 60, 240] as const;
/**
 * A TA snapshot is past its limit when its candle closed more than one timeframe plus this allowance ago: by then
 * the next candle has closed and the machine's monitor scheduler (about once a minute) should have saved a newer one.
 */
export const TA_RUN_ALLOWANCE_MS = 10 * 60_000;

export { MOVEMENT_POLL_INTERVAL_MS };
/** An age within this much past its limit is still graded fresh (timer jitter, a request in flight). */
export const FRESH_DISPLAY_GRACE_MS = 3_000;
/** After a refetch finishes, an item still past its limit reads "catching up" for this long before "stale". */
export const CATCH_UP_WINDOW_MS = 15_000;

/** Past a limit, after the display grace. */
export const pastLimit = (ageMs: number, limitMs: number) => ageMs > limitMs + FRESH_DISPLAY_GRACE_MS;

/**
 * Where the engine state came from, or why it is missing; separate from the engine's own status.
 * ok: read from the operational store; published: the machine's published copy in the hosted database;
 * published_none: no copy has been published yet.
 */
export type MovementReadStatus =
  | "ok" | "published" | "published_none" | "store_disabled" | "read_failed" | "not_loaded";

export type StatusTone = "live" | "warming" | "stale" | "offline" | "unavailable";

/** The state was read (directly or as a published copy), so its numbers can be mapped. */
const readable = (read: MovementReadStatus) => read === "ok" || read === "published";

// ---------------------------------------------------------------- shared helpers

export function formatAge(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds <= 90) return `${seconds} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h` : `${Math.round(hours / 24)} days`;
}

const metricValue = (metric: Metric | undefined): number | null =>
  metric && metric.available ? metric.value : null;
const classifierValue = <T>(metric: ClassifierMetric<T> | undefined): T | null =>
  metric && metric.available ? metric.value : null;
const pct = (logReturn: number) => (Math.exp(logReturn) - 1) * 100;

/**
 * The engine's own flat/material thresholds for the saved movement config version, or null when the saved
 * evidence was produced under a config this build does not know (then no number is claimed).
 */
export function engineThresholds(configVersion: string | null | undefined): { flatZ: number; materialZ: number } | null {
  return configVersion === DEFAULT_MARKET_MOVEMENT_CONFIG.version
    ? { flatZ: DEFAULT_MARKET_MOVEMENT_CONFIG.flatZ, materialZ: DEFAULT_MARKET_MOVEMENT_CONFIG.materialZ }
    : null;
}

const DIRECTION_LABEL: Record<string, string> = {
  BROAD_RISE: "Broad rise",
  BROAD_DROP: "Broad drop",
  NEUTRAL: "No clear broad move",
  WARMING: "Collecting live history",
  UNAVAILABLE: "Unavailable",
};
const PACE_LABEL: Record<string, string> = {
  ACCELERATING: "speeding up",
  DECELERATING: "slowing down",
  MIXED: "pace mixed",
};

// ---------------------------------------------------------------- market strip

export type WindowSummary = {
  minutes: number;
  available: boolean;
  /** Why the breadth is missing, when it is. */
  reason: string | null;
  denominator: number;
  rising: number | null;
  falling: number | null;
  materialRising: number | null;
  materialFalling: number | null;
  /** "4 of 6 pairs fell materially in 15 min" (or the plain breadth when no material move). */
  sentence: string;
  /** The tile's muted line: "4 of 6 falling" in the direction of the median move. */
  short: string;
  /** What "material" means, with the engine's value when it is known for the saved config. */
  materialNote: string;
  medianMovePct: number | null;
  medianNormalized: number | null;
  dispersion: number | null;
  outliers: Array<{ symbol: string; direction: "rising" | "falling"; movePct: number; crossSectionalZ: number }>;
};

export type MarketStripView = {
  /** Where the state came from (see MovementReadStatus). */
  read: MovementReadStatus;
  /**
   * The engine's status, judged on the machine's own clock: STALE when the machine saved or published an
   * evaluation more than the engine's stale window after its boundary. Browser-side age never makes it STALE.
   */
  status: MovementEngineStatus;
  /** The machine stopped reporting: nothing newer than PUBLISHED_COPY_OFFLINE_AFTER_MS reached the browser. */
  machineOffline: boolean;
  /** Machine-side lateness of the evaluation (published or saved time minus boundary), ms; null when unknown. */
  machineLateMs: number | null;
  /** When the machine last published (published copy) or saved (direct read) the state, ms. */
  reportedAt: number | null;
  /** Where the numbers come from, in plain words (always filled). */
  source: string;
  /** "As of hh:mm, not live" for everything that is not LIVE and current; null when the numbers are current. */
  notLiveNote: string | null;
  tone: StatusTone;
  statusLabel: string;
  /** One sentence on what the status means for the numbers below; never empty. */
  explanation: string;
  /** True only for LIVE: numbers may be read as current. Anything else is shown with an "as of" caption. */
  current: boolean;
  direction: { label: string; state: string } | null;
  pace: string | null;
  windows: WindowSummary[];
  universe: {
    configured: string[];
    included: string[];
    excluded: Array<{ symbol: string; reasons: string[] }>;
  } | null;
  times: {
    evaluationBoundary: number | null;
    lastReceivedAt: number | null;
    lastSourceEvent: number | null;
    engineUpdatedAt: number | null;
  };
  evaluationAge: string | null;
};

function windowSummary(window: MarketClassificationWindow, thresholds: ReturnType<typeof engineThresholds>): WindowSummary {
  const b = window.breadth;
  const denominator = b?.denominator ?? 0;
  const count = (metric: Metric<{ count: number }> | undefined) => (metric && metric.available ? metric.value.count : null);
  const rising = count(b?.rising);
  const falling = count(b?.falling);
  const materialRising = count(b?.materialRising);
  const materialFalling = count(b?.materialFalling);
  const available = Boolean(b?.available) && denominator > 0;
  const m = window.windowMinutes;
  let sentence: string;
  if (!available) {
    sentence = `No pair count for ${m} min: ${humanizeToken(b?.unavailableReason ?? window.availabilityReasons?.[0] ?? "unavailable").toLowerCase()}.`;
  } else {
    const parts: string[] = [];
    if (materialRising !== null && materialFalling !== null) {
      if (materialFalling >= materialRising && materialFalling > 0) parts.push(`${materialFalling} of ${denominator} pairs fell materially in ${m} min`);
      else if (materialRising > 0) parts.push(`${materialRising} of ${denominator} pairs rose materially in ${m} min`);
      else parts.push(`No pair moved materially in ${m} min`);
    }
    parts.push(`${rising ?? "?"} rising, ${falling ?? "?"} falling of ${denominator}`);
    sentence = parts.join(" · ");
  }
  const materialNote = thresholds
    ? `Material means the pair's ${m}-minute move is at least ${thresholds.materialZ} usual-sized moves (robust z-score) away from its normal; flat is under ${thresholds.flatZ}. These are the engine's own values (${window.movementConfigVersion}).`
    : `Material uses the engine's own threshold from config ${window.movementConfigVersion}, which this page does not know the value of.`;
  const median = metricValue(window.medianRawReturn);
  const leanFalling = median !== null ? median < 0 : (falling ?? 0) > (rising ?? 0);
  const short = !available
    ? "No count"
    : leanFalling ? `${falling ?? "?"} of ${denominator} falling` : `${rising ?? "?"} of ${denominator} rising`;
  return {
    minutes: m,
    available,
    reason: available ? null : (b?.unavailableReason ?? window.availabilityReasons?.[0] ?? "unavailable"),
    denominator,
    rising,
    falling,
    materialRising,
    materialFalling,
    sentence,
    short,
    materialNote,
    medianMovePct: median === null ? null : pct(median),
    medianNormalized: metricValue(window.medianNormalizedMovement),
    dispersion: metricValue(window.dispersionMadNormalizedMovement),
    outliers: (window.isolatedOutliers ?? []).map((o) => ({
      symbol: o.symbol,
      direction: o.direction === "RISING" ? "rising" : "falling",
      movePct: pct(o.rawReturn),
      crossSectionalZ: o.crossSectionalZ,
    })),
  };
}

/** The unavailable reason, in plain language, for a state that is UNAVAILABLE. */
function unavailableReason(state: MarketMovementCurrentState, read: MovementReadStatus): string {
  if (read === "not_loaded") return "Not loaded yet. Press Refresh if automatic refresh is paused.";
  if (read === "published_none") {
    return "Your machine has not published a market state yet. Start the worker host with publishing on (see docs/worker-host.md); per-pair prices and technical analysis below are unaffected.";
  }
  if (read === "store_disabled") {
    return "This site cannot reach the market engine: it runs on your machine's operational database, which this deployment does not read. Per-pair prices and technical analysis below are unaffected.";
  }
  if (read === "read_failed") return "The market engine state could not be read just now. Try again in a minute.";
  if (!state.available) return "The market engine has not saved a market state yet (it may still be starting).";
  const configured = state.universe?.configuredSymbols.length ?? 0;
  if (configured < MINIMUM_MARKET_UNIVERSE_SIZE) {
    return `Market-wide state needs at least ${MINIMUM_MARKET_UNIVERSE_SIZE} watched contracts; the engine has ${configured}. Per-pair rows still show.`;
  }
  const reasons = state.primary?.evidence.availabilityReasons ?? [];
  return reasons.length
    ? `The engine could not form a market-wide state: ${reasons.map((r) => humanizeToken(r).toLowerCase()).join(", ")}.`
    : "The engine could not form a market-wide state from the pairs it has.";
}

const clock = (ms: number) => new Date(ms).toLocaleTimeString();
/** "5:43 PM": the short clock used in user-facing sentences. */
export const shortClock = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

/** When the machine last published (published copy) or saved (direct read) this state. */
export function machineReportedAt(
  state: MarketMovementCurrentState,
  read: MovementReadStatus,
  publishedAt: number | null,
): number | null {
  if (read === "published") return publishedAt;
  if (read === "ok") return state.timestamps?.engineUpdatedAt ?? null;
  return null;
}

/** Machine-side lateness of the evaluation: reported time minus evaluation boundary. The browser clock plays no part. */
export function machineLatenessMs(
  state: MarketMovementCurrentState,
  read: MovementReadStatus,
  publishedAt: number | null,
): number | null {
  const boundary = state.timestamps?.evaluationBoundaryTime ?? null;
  const reported = machineReportedAt(state, read, publishedAt);
  return boundary === null || reported === null ? null : Math.max(0, reported - boundary);
}

/**
 * The machine has gone silent: nothing it published or saved reached the browser within one publish interval,
 * one poll and a margin (PUBLISHED_COPY_OFFLINE_AFTER_MS).
 */
export function machineSilent(
  state: MarketMovementCurrentState,
  read: MovementReadStatus,
  publishedAt: number | null,
  now: number,
): boolean {
  const reported = machineReportedAt(state, read, publishedAt);
  return reported !== null && now - reported > PUBLISHED_COPY_OFFLINE_AFTER_MS;
}

/**
 * @param read where the state came from (see MovementReadStatus)
 * @param publishedAt for a published copy: when the machine last reported (publish or heartbeat), in ms
 */
export function buildMarketStrip(
  state: MarketMovementCurrentState,
  now: number,
  read: MovementReadStatus = "ok",
  publishedAt: number | null = null,
): MarketStripView {
  const boundary = state.timestamps?.evaluationBoundaryTime ?? null;
  const published = read === "published";
  const machineOffline = readable(read) && machineSilent(state, read, publishedAt, now);
  const machineLateMs = readable(read) ? machineLatenessMs(state, read, publishedAt) : null;
  // The engine's status on the machine's own clock: late only when the machine published or saved the evaluation
  // more than the engine's stale window after its boundary. The poll and publish delays never count; a machine
  // that went silent is reported as offline instead.
  let engineStatus: MovementEngineStatus = state.status;
  if (machineLateMs !== null && state.status !== "UNAVAILABLE") {
    if (pastLimit(machineLateMs, MOVEMENT_ENGINE_STALE_AFTER_MS)) engineStatus = "STALE";
    else if (state.status === "STALE") {
      const d = state.primary?.directionState;
      engineStatus = d === "WARMING" ? "WARMING" : d === "UNAVAILABLE" ? "UNAVAILABLE" : "LIVE";
    }
  }
  const status: MovementEngineStatus = readable(read) ? engineStatus : "UNAVAILABLE";
  const age = boundary === null ? null : formatAge(now - boundary);
  const source = published
    ? `Published copy from your machine${publishedAt === null ? "" : `, last reported ${formatAge(now - publishedAt)} ago`}`
    : read === "ok"
      ? "Read live from the market engine's database"
      : "No data source";
  const base = {
    read,
    status,
    machineOffline,
    machineLateMs,
    reportedAt: readable(read) ? machineReportedAt(state, read, publishedAt) : null,
    source,
    times: {
      evaluationBoundary: boundary,
      lastReceivedAt: state.timestamps?.lastReceivedAt ?? null,
      lastSourceEvent: state.timestamps?.lastSourceEventTime ?? null,
      engineUpdatedAt: state.timestamps?.engineUpdatedAt ?? null,
    },
    evaluationAge: age,
  };
  const universe = state.universe
    ? {
        configured: state.universe.configuredSymbols,
        included: state.universe.includedSymbols,
        excluded: state.universe.excludedSymbols.map((e) => ({
          symbol: e.symbol,
          reasons: e.reasons.map((r) => humanizeToken(r)),
        })),
      }
    : null;
  const windows = ASSESSMENT_WINDOWS.flatMap((minutes) => {
    const window = state.context.find((w) => w.windowMinutes === minutes);
    return window ? [windowSummary(window, engineThresholds(window.movementConfigVersion))] : [];
  });
  const primary = state.primary;
  const direction = primary ? { label: DIRECTION_LABEL[primary.directionState] ?? humanizeToken(primary.directionState), state: primary.directionState } : null;
  const paceValue = primary ? classifierValue(primary.evidence.pace) : null;
  const pace = paceValue ? (PACE_LABEL[paceValue] ?? humanizeToken(paceValue).toLowerCase()) : null;
  const asOf = boundary === null ? "" : `As of ${clock(boundary)} (${age} ago)`;

  if (status === "UNAVAILABLE") {
    return { ...base, tone: "unavailable", statusLabel: "Unavailable", current: false, notLiveNote: null,
      explanation: unavailableReason(state, read), direction: null, pace: null, windows: [], universe };
  }
  if (machineOffline) {
    return { ...base, tone: "offline", statusLabel: "Machine offline", current: false,
      notLiveNote: `${asOf}, not live: machine offline`,
      explanation: `Your machine has not reported for ${formatAge(now - (base.reportedAt ?? now))} (it reports every ${formatAge(MOVEMENT_PUBLISH_INTERVAL_MS)}; offline after ${formatAge(PUBLISHED_COPY_OFFLINE_AFTER_MS)}). This is the last ${published ? "published copy" : "saved state"}, not live.`,
      direction, pace, windows, universe };
  }
  if (status === "STALE") {
    return { ...base, tone: "stale", statusLabel: "Stale", current: false, notLiveNote: `${asOf}, not live`,
      explanation: `Not live: the last evaluation was ${formatAge(machineLateMs ?? 0)} old when your machine ${published ? "published" : "saved"} it (limit ${formatAge(MOVEMENT_ENGINE_STALE_AFTER_MS)}). The numbers below are as of then.`,
      direction, pace, windows, universe };
  }
  if (status === "WARMING") {
    return { ...base, tone: "warming", statusLabel: "Warming up", current: false, notLiveNote: "Provisional while the engine warms up",
      explanation: "The engine is collecting live 5-second history; the market-wide state appears once there is enough.",
      direction, pace, windows, universe };
  }
  return { ...base, tone: "live", statusLabel: "Live", current: true, notLiveNote: null,
    explanation: `Evaluated ${age} ago on ${universe?.included.length ?? 0} of ${universe?.configured.length ?? 0} watched pairs.`,
    direction, pace, windows, universe };
}

/** What the strip's single status line says. */
export type StripStatusKind = "live" | "updating" | "warming" | "offline" | "late" | "failed" | "unavailable" | "loading";

/**
 * One status for the strip from the view and the graded movement/stream freshness. A short lag (a refresh in
 * flight, a tab just resumed) reads "updating", never a fault; "late" and "failed" only follow a refresh attempt.
 */
export function stripStatus(view: MarketStripView, grade: FreshnessGrade, settling: boolean, lastAttemptFailed: boolean): StripStatusKind {
  if (view.read === "not_loaded") return "loading";
  if (view.machineOffline) return "offline";
  if (settling) return "updating";
  if (view.read === "read_failed" || (lastAttemptFailed && grade === "stale")) return "failed";
  if (view.tone === "unavailable") return "unavailable";
  if (grade === "catching_up") return "updating";
  if (grade === "stale") return "late";
  if (view.tone === "warming") return "warming";
  return "live";
}

/** A short plain sentence for an unavailable strip; the full reason stays in Diagnostics. */
export function unavailableShort(view: MarketStripView): string {
  if (view.read === "store_disabled") return "The market reading isn't available on this site.";
  if (view.read === "published_none") return "Your machine hasn't sent a market reading yet.";
  if (view.read === "read_failed") return "The market reading couldn't be loaded.";
  return "The market reading isn't ready yet.";
}

/** "Broad rise, speeding up" with its tone (green only for a confirmed rise, red only for a confirmed drop). */
export function stripHeadline(view: MarketStripView): { text: string; tone: "up" | "down" | "neutral" } | null {
  if (!view.direction) return null;
  const tone = view.direction.state === "BROAD_RISE" ? "up" : view.direction.state === "BROAD_DROP" ? "down" : "neutral";
  return { text: `${view.direction.label}${view.pace ? `, ${view.pace}` : ""}`, tone };
}

/** "7 of 9 pairs rose in the last 15 min" from the 15-minute window; null when it has no count. */
export function stripHeadlineDetail(view: MarketStripView): string | null {
  const w = view.windows.find((x) => x.minutes === 15);
  if (!w || !w.available || w.materialRising === null || w.materialFalling === null) return null;
  if (w.materialFalling >= w.materialRising && w.materialFalling > 0) return `${w.materialFalling} of ${w.denominator} pairs fell in the last ${w.minutes} min`;
  if (w.materialRising > 0) return `${w.materialRising} of ${w.denominator} pairs rose in the last ${w.minutes} min`;
  return `No pair moved much in the last ${w.minutes} min`;
}

// ---------------------------------------------------------------- per-pair movement

export type PairWindowView = {
  minutes: number;
  available: boolean;
  reason: string | null;
  z: number | null;
  zText: string;
  /** How unusual the move is, in the engine's own flat / material bands. */
  label: string;
  tone: "up" | "down" | "flat" | "unknown";
  material: boolean;
};

export type PairMovementView =
  | { kind: "unavailable"; reason: string }
  | { kind: "excluded"; reasons: string[] }
  | {
      kind: "ok";
      windows: PairWindowView[];
      rvol: { value: number; text: string } | null;
      rvolReason: string | null;
      outlier: { direction: "rising" | "falling"; window: number; text: string } | null;
      /** Some window excludes the pair (its reasons are named), though others may show. */
      partialReasons: string[];
      /** "As of hh:mm, not live" when the engine state is stale, warming or a copy of an offline machine; else null. */
      notLive: string | null;
    };

function zLabel(z: number, thresholds: ReturnType<typeof engineThresholds>, material: boolean): { label: string; tone: PairWindowView["tone"] } {
  const direction = z > 0 ? "up" : "down";
  if (!thresholds) return { label: `${z > 0 ? "above" : "below"} its normal (config unknown)`, tone: direction };
  const size = Math.abs(z);
  if (size < thresholds.flatZ) return { label: "about normal", tone: "flat" };
  if (material || size >= thresholds.materialZ) return { label: z > 0 ? "unusually strong rise" : "unusually sharp drop", tone: direction };
  return { label: z > 0 ? "a little stronger than usual" : "a little weaker than usual", tone: direction };
}

export function buildPairMovement(
  symbol: string,
  state: MarketMovementCurrentState,
  read: MovementReadStatus = "ok",
  notLive: string | null = null,
): PairMovementView {
  if (!readable(read) || !state.available) return { kind: "unavailable", reason: unavailableReason(state, read) };
  if (state.universe && !state.universe.configuredSymbols.includes(symbol)) {
    return { kind: "unavailable", reason: "Not in the engine's market universe yet; it is synced from your watchlist every minute or so." };
  }
  const excluded = state.universe?.excludedSymbols.find((e) => e.symbol === symbol);
  const windows: PairWindowView[] = [];
  const partial = new Set<string>();
  let rvol: { value: number; text: string } | null = null;
  let rvolReason: string | null = null;
  let outlier: { direction: "rising" | "falling"; window: number; text: string } | null = null;
  for (const minutes of ASSESSMENT_WINDOWS) {
    const window = state.context.find((w) => w.windowMinutes === minutes);
    const metrics = window?.movementSnapshot?.symbols?.find((s) => s.symbol === symbol);
    if (!window || !metrics) {
      windows.push({ minutes, available: false, reason: "no evidence for this window", z: null, zText: "—", label: "not available", tone: "unknown", material: false });
      continue;
    }
    const z = metricValue(metrics.normalizedZ);
    if (!metrics.included || z === null) {
      const reasons = metrics.exclusionReasons?.length
        ? metrics.exclusionReasons.map((r) => humanizeToken(r).toLowerCase())
        : [humanizeToken(metrics.normalizedZ.available ? "unavailable" : metrics.normalizedZ.reason).toLowerCase()];
      reasons.forEach((r) => partial.add(r));
      windows.push({ minutes, available: false, reason: reasons.join(", "), z: null, zText: "—", label: "not available", tone: "unknown", material: false });
    } else {
      const material = metrics.materialRising || metrics.materialFalling;
      const { label, tone } = zLabel(z, engineThresholds(window.movementConfigVersion), material);
      windows.push({ minutes, available: true, reason: null, z, zText: `${z > 0 ? "+" : ""}${z.toFixed(2)}`, label, tone, material });
    }
    if (minutes === 5) {
      const value = metricValue(metrics.rvol);
      if (value !== null) rvol = { value, text: `${value.toFixed(2)}× its usual volume` };
      else rvolReason = humanizeToken(metrics.rvol.available ? "unavailable" : metrics.rvol.reason).toLowerCase();
    }
    if (metrics.outlierCandidate && window.isolatedOutliers?.some((o) => o.symbol === symbol)) {
      const o = window.isolatedOutliers.find((x) => x.symbol === symbol)!;
      const direction = o.direction === "RISING" ? "rising" : "falling";
      if (!outlier || window.windowMinutes === 5) {
        outlier = { direction, window: window.windowMinutes, text: `Isolated ${direction === "rising" ? "riser" : "faller"} in ${window.windowMinutes} min: the move is not shared by the rest of the market` };
      }
    }
  }
  if (excluded && windows.every((w) => !w.available)) {
    return { kind: "excluded", reasons: excluded.reasons.map((r) => humanizeToken(r)) };
  }
  return { kind: "ok", windows, rvol, rvolReason, outlier, partialReasons: [...partial], notLive };
}

// ---------------------------------------------------------------- completed-candle TA per timeframe

export type TaRowInput = {
  symbol: string;
  timeframe: number;
  candle_at: string;
  detected_at?: string | null;
  price: number;
  classification: string;
  score: number | null;
  atr_pct: number | null;
  indicators: unknown;
  factor_breakdown: unknown;
  patterns: string[];
};

export type TaFrameView = {
  timeframe: number;
  label: string;
  status: "ok" | "stale" | "missing" | "error" | "not_loaded";
  lean: "bullish" | "bearish" | "neutral" | "unknown";
  leanText: string;
  score: number | null;
  rsi: number | null;
  trend: string | null;
  atrPct: number | null;
  patterns: string[];
  candleClosedAt: number | null;
  savedAt: number | null;
  /** Always filled: what the user should know about this snapshot (age, staleness, why missing). */
  note: string;
};

const record = (v: unknown): Record<string, unknown> =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
export const taFrameLabel = (timeframe: number) => (timeframe === 15 ? "15m" : `${timeframe / 60}h`);
/** How long after its candle closed a TA snapshot stays current: one timeframe plus the run allowance. */
export const taLimitMs = (timeframe: number) => timeframe * 60_000 + TA_RUN_ALLOWANCE_MS;

export function buildTaFrame(
  row: TaRowInput | undefined,
  timeframe: number,
  now: number,
  load: "loaded" | "failed" | "not_loaded" = "loaded",
): TaFrameView {
  const label = taFrameLabel(timeframe);
  const empty = { timeframe, label, lean: "unknown" as const, leanText: "No reading", score: null, rsi: null, trend: null, atrPct: null,
    patterns: [] as string[], candleClosedAt: null, savedAt: null };
  if (load === "failed" && !row) return { ...empty, status: "error", note: "Could not load the technical analysis snapshot." };
  if (load === "not_loaded" && !row) {
    return { ...empty, status: "not_loaded", note: "Not loaded yet. Press Refresh if automatic refresh is paused." };
  }
  if (!row) {
    return { ...empty, status: "missing", note: "No snapshot saved yet. One is saved after each completed candle once monitoring and technical analysis are on." };
  }
  const closedAt = Date.parse(row.candle_at) + timeframe * 60_000;
  const savedAt = row.detected_at ? Date.parse(row.detected_at) : null;
  const behind = now - closedAt;
  const stale = pastLimit(behind, taLimitMs(timeframe));
  const lean = row.classification === "bullish" ? "bullish" : row.classification === "bearish" ? "bearish" : row.classification === "neutral" ? "neutral" : "unknown";
  const indicators = record(row.indicators);
  const rsi = indicators["rsi14"];
  const trend = record(record(row.factor_breakdown)["trend"])["classification"];
  return {
    timeframe,
    label,
    status: stale ? "stale" : "ok",
    lean,
    leanText: lean === "bullish" ? "Bullish lean" : lean === "bearish" ? "Bearish lean" : lean === "neutral" ? "Neutral" : "Unknown",
    score: typeof row.score === "number" ? row.score : null,
    rsi: typeof rsi === "number" && Number.isFinite(rsi) ? rsi : null,
    trend: typeof trend === "string" ? humanizeToken(trend) : null,
    atrPct: typeof row.atr_pct === "number" ? row.atr_pct : null,
    patterns: (row.patterns ?? []).map((p) => humanizeToken(p)),
    candleClosedAt: closedAt,
    savedAt: Number.isFinite(savedAt ?? NaN) ? savedAt : null,
    note: stale
      ? `Waiting for a newer snapshot: the candle closed ${formatAge(behind)} ago and a newer one has closed since.`
      : `Candle closed ${formatAge(Math.max(0, behind))} ago${savedAt ? `, snapshot saved ${formatAge(Math.max(0, now - savedAt))} ago` : ""}.`,
  };
}

/** Latest row per (symbol, timeframe) from rows ordered newest first. */
export function latestTaBySymbol(rows: readonly TaRowInput[]): Map<string, TaRowInput> {
  const latest = new Map<string, TaRowInput>();
  for (const row of rows) {
    const key = `${row.symbol}|${row.timeframe}`;
    const current = latest.get(key);
    if (!current || Date.parse(row.candle_at) > Date.parse(current.candle_at)) latest.set(key, row);
  }
  return latest;
}

// ---------------------------------------------------------------- freshness per source

export type FreshnessItem = {
  id: "stream" | "movement" | "candles" | "ta";
  label: string;
  /**
   * The raw state of the data alone: "stale" means past its limit (after FRESH_DISPLAY_GRACE_MS) or the machine
   * is offline. What the page shows is the grade from `gradeFreshness`, which also weighs the refresh state.
   */
  status: "fresh" | "stale" | "unavailable";
  detail: string;
  asOf: number | null;
  /** How far past its limit the item is measured, its limit, and when it crossed (null when not applicable). */
  ageMs: number | null;
  limitMs: number | null;
  crossedAt: number | null;
  /** The machine stopped reporting (movement and stream only). */
  offline: boolean;
};

export type FreshnessInput = {
  state: MarketMovementCurrentState;
  read: MovementReadStatus;
  symbols: readonly string[];
  quotes: ReadonlyArray<{ symbol: string; ok: boolean; stale: boolean; lastCandleAt: number | null }>;
  /** Latest TA rows (any order); undefined while not loaded. */
  ta: readonly TaRowInput[] | undefined;
  /** For a published copy: when the machine last reported (ms). */
  publishedAt?: number | null;
};

const unmeasured = { ageMs: null, limitMs: null, crossedAt: null, offline: false } as const;

/**
 * Four separate source lines. The stream and the movement evaluation are judged on the machine's own timestamps
 * (evaluation boundary, saved/published time), so the publish and poll cadence alone never makes them stale;
 * completed candles and TA snapshots are judged against `now`, with limits far longer than that cadence.
 */
export function buildFreshness(input: FreshnessInput, now: number): FreshnessItem[] {
  const { state, read, symbols, quotes, ta, publishedAt = null } = input;
  const items: FreshnessItem[] = [];
  const engineLimit = formatAge(MOVEMENT_ENGINE_STALE_AFTER_MS);
  const boundary = state.timestamps?.evaluationBoundaryTime ?? null;
  const offline = readable(read) && machineSilent(state, read, publishedAt, now);

  // Market stream: the engine's per-symbol receive times, measured back from its own evaluation boundary.
  const primary = state.context.find((w) => w.windowMinutes === 5);
  if (!readable(read) || !primary || boundary === null) {
    items.push({ id: "stream", label: "Market stream", status: "unavailable", ...unmeasured,
      detail: read === "store_disabled" ? "Not readable from this site." : read === "published_none" ? "No published copy yet." : "No stream evidence from the engine.", asOf: null });
  } else {
    const evidence = primary.sourceTimeEvidence ?? [];
    const watched = symbols.length ? symbols : evidence.map((e) => e.symbol);
    const lagging: string[] = [];
    let newest: number | null = null;
    let oldest: number | null = null;
    let missingAny = false;
    for (const symbol of watched) {
      const at = evidence.find((e) => e.symbol === symbol)?.lastReceivedAtMs ?? null;
      if (at === null || pastLimit(boundary - at, MOVEMENT_ENGINE_STALE_AFTER_MS)) lagging.push(symbol.replace(/USDT$/, ""));
      if (at === null) missingAny = true;
      if (at !== null && (newest === null || at > newest)) newest = at;
      if (at !== null && (oldest === null || at < oldest)) oldest = at;
    }
    const unknown = missingAny || oldest === null;
    items.push({ id: "stream", label: "Market stream", asOf: newest,
      status: newest === null || lagging.length > 0 || offline ? "stale" : "fresh",
      ageMs: unknown ? Number.POSITIVE_INFINITY : Math.max(0, boundary - oldest!),
      limitMs: MOVEMENT_ENGINE_STALE_AFTER_MS,
      crossedAt: unknown ? boundary : oldest! + MOVEMENT_ENGINE_STALE_AFTER_MS,
      offline,
      detail: lagging.length
        ? `No trade data within ${engineLimit} of the evaluation for ${lagging.join(", ")}${newest === null ? "" : `; newest ${formatAge(now - newest)} ago`}.`
        : `Every watched pair received data within ${engineLimit} of the evaluation (newest ${formatAge(now - (newest ?? now))} ago).` });
  }

  // Movement evaluation.
  if (!readable(read) || boundary === null) {
    items.push({ id: "movement", label: "Movement evaluation", status: "unavailable", ...unmeasured,
      detail: read === "store_disabled" ? "Not readable from this site." : read === "published_none" ? "No published copy yet." : "No evaluation saved yet.", asOf: null });
  } else {
    const lateness = machineLatenessMs(state, read, publishedAt);
    const old = lateness !== null && pastLimit(lateness, MOVEMENT_ENGINE_STALE_AFTER_MS);
    const reported = machineReportedAt(state, read, publishedAt);
    const copy = read === "published"
      ? `; published copy, machine ${offline ? `OFFLINE (last reported ${formatAge(now - (reported ?? now))} ago)` : `reported ${formatAge(now - (reported ?? now))} ago`}`
      : "";
    items.push({ id: "movement", label: "Movement evaluation", asOf: boundary, status: old || offline ? "stale" : "fresh",
      ageMs: lateness, limitMs: MOVEMENT_ENGINE_STALE_AFTER_MS, crossedAt: boundary + MOVEMENT_ENGINE_STALE_AFTER_MS, offline,
      detail: `Last evaluation ${formatAge(now - boundary)} ago, ${formatAge(lateness ?? 0)} old when ${read === "published" ? "published" : "saved"} (stale after ${engineLimit}); engine status ${(old && state.status === "LIVE" ? "STALE" : state.status).toLowerCase()}${copy}.` });
  }

  // Completed candles behind the price cards: against now (QUOTE_STALE_MS is far longer than the poll).
  const candleQuotes = quotes.filter((q) => symbols.length === 0 || symbols.includes(q.symbol));
  if (candleQuotes.length === 0) {
    items.push({ id: "candles", label: "Completed candles", status: "unavailable", ...unmeasured, detail: "No quotes loaded yet.", asOf: null });
  } else {
    const stale = candleQuotes.filter((q) => !q.ok || q.stale || q.lastCandleAt === null || pastLimit(now - q.lastCandleAt, QUOTE_STALE_MS));
    const times = candleQuotes.flatMap((q) => (q.lastCandleAt === null ? [] : [q.lastCandleAt]));
    const oldest = times.length ? Math.min(...times) : null;
    // A quote the server already flagged counts as past its limit at once.
    const flagged = candleQuotes.some((q) => !q.ok || q.stale || q.lastCandleAt === null);
    items.push({ id: "candles", label: "Completed candles", asOf: oldest, status: stale.length ? "stale" : "fresh",
      ageMs: flagged || oldest === null ? Number.POSITIVE_INFINITY : now - oldest, limitMs: QUOTE_STALE_MS,
      crossedAt: flagged || oldest === null ? null : oldest + QUOTE_STALE_MS, offline: false,
      detail: stale.length
        ? `Old or missing for ${stale.map((q) => q.symbol.replace(/USDT$/, "")).join(", ")} (older than ${formatAge(QUOTE_STALE_MS)}).`
        : `Newest candle at most ${oldest === null ? "?" : formatAge(now - oldest)} old on every pair.` });
  }

  // TA snapshots.
  if (ta === undefined) {
    items.push({ id: "ta", label: "Technical analysis", status: "unavailable", ...unmeasured, detail: "Not loaded yet.", asOf: null });
  } else {
    const latest = latestTaBySymbol(ta);
    const stale: string[] = [];
    const missing: string[] = [];
    let oldestClose: number | null = null;
    // The frame closest to (or furthest past) its limit decides the item's age and crossing.
    let worst: { over: number; ageMs: number; limitMs: number; crossedAt: number } | null = null;
    for (const symbol of symbols) {
      for (const timeframe of TA_ASSESSMENT_FRAMES) {
        const row = latest.get(`${symbol}|${timeframe}`);
        const view = buildTaFrame(row, timeframe, now);
        const name = `${symbol.replace(/USDT$/, "")} ${view.label}`;
        if (view.status === "missing") missing.push(name);
        else if (view.status === "stale") stale.push(name);
        if (view.candleClosedAt !== null) {
          if (oldestClose === null || view.candleClosedAt < oldestClose) oldestClose = view.candleClosedAt;
          const limitMs = taLimitMs(timeframe);
          const ageMs = now - view.candleClosedAt;
          if (!worst || ageMs - limitMs > worst.over) worst = { over: ageMs - limitMs, ageMs, limitMs, crossedAt: view.candleClosedAt + limitMs };
        }
      }
    }
    const problems = [
      ...(stale.length ? [`stale: ${stale.join(", ")}`] : []),
      ...(missing.length ? [`no snapshot: ${missing.join(", ")}`] : []),
    ];
    items.push({ id: "ta", label: "Technical analysis", asOf: oldestClose,
      status: problems.length ? (symbols.length && missing.length === symbols.length * TA_ASSESSMENT_FRAMES.length ? "unavailable" : "stale") : "fresh",
      ageMs: worst?.ageMs ?? null, limitMs: worst?.limitMs ?? null, crossedAt: worst?.crossedAt ?? null, offline: false,
      detail: problems.length ? problems.join("; ") : "Every pair has a current snapshot for 15m, 1h and 4h." });
  }
  return items;
}

// ---------------------------------------------------------------- grades: fresh, catching up, stale

export type FreshnessGrade = "fresh" | "catching_up" | "stale";

export type GradeInput = {
  /** How old the datum is, measured the way its limit is (see FreshnessItem). Infinity = known to be past. */
  ageMs: number;
  limitMs: number;
  /** A refetch of this datum is in flight. */
  refreshing: boolean;
  /** When the last refetch of this datum finished (success or error), ms; null when none yet. */
  lastRefreshAt: number | null;
  /** The last refetch failed. */
  lastAttemptFailed: boolean;
  now: number;
  /** When the datum crossed its limit; defaults to now - ageMs + limitMs (an age that grows with now). */
  crossedAt?: number | null;
  /** The machine stopped reporting. */
  offline?: boolean;
  /** A self-heal check is running, or the tab just resumed and its refetch has not landed. */
  healing?: boolean;
  /** False when nothing will refresh the datum on its own (automatic refresh paused). */
  canRefresh?: boolean;
};

/**
 * Stale is a fault report, never the normal state. A datum past its limit is "catching up" while a refresh is in
 * flight or about to be (none attempted since it crossed), during CATCH_UP_WINDOW_MS after a refetch, or while a
 * self-heal or resume refetch runs. It is "stale" only when a refresh after the crossing still left it past its
 * limit, when the refresh failed, or when the machine is offline.
 */
export function gradeFreshness(input: GradeInput): FreshnessGrade {
  const { ageMs, limitMs, refreshing, lastRefreshAt, lastAttemptFailed, now, offline = false, healing = false, canRefresh = true } = input;
  if (offline) return "stale";
  const past = pastLimit(ageMs, limitMs);
  if (!past && !lastAttemptFailed) return "fresh";
  if (refreshing || healing) return "catching_up";
  if (lastAttemptFailed) return "stale";
  if (!canRefresh) return "stale";
  const crossedAt = input.crossedAt ?? (Number.isFinite(ageMs) ? now - ageMs + limitMs : null);
  if (lastRefreshAt === null || (crossedAt !== null && lastRefreshAt <= crossedAt)) return "catching_up";
  if (now - lastRefreshAt < CATCH_UP_WINDOW_MS) return "catching_up";
  return "stale";
}

/**
 * The soonest moment one of the data crosses its limit (each `asOf + limitMs`), when it is in the future; else
 * null. The page arms one refetch for that moment, so a datum is refreshed as it crosses instead of waiting for the
 * next interval poll (which could leave it past its limit for up to a whole interval).
 */
export function nextLimitCrossing(
  data: ReadonlyArray<{ asOf: number | null; limitMs: number }>,
  now: number,
): number | null {
  let soonest: number | null = null;
  for (const { asOf, limitMs } of data) {
    if (asOf === null) continue;
    const crossing = asOf + limitMs + FRESH_DISPLAY_GRACE_MS;
    if (crossing > now && (soonest === null || crossing < soonest)) soonest = crossing;
  }
  return soonest;
}

// ---------------------------------------------------------------- per-pair summary

/** The movement chip on a pair card: the 15m label, capitalised, with its tone. */
export function pairMovementChip(movement: PairMovementView): { text: string; tone: PairWindowView["tone"] } {
  if (movement.kind === "unavailable") return { text: "No market reading", tone: "unknown" };
  if (movement.kind === "excluded") return { text: "Left out of the market reading", tone: "unknown" };
  const w = movement.windows.find((x) => x.minutes === 15);
  if (!w || !w.available) return { text: "15m move unavailable", tone: "unknown" };
  return { text: `${w.label.charAt(0).toUpperCase()}${w.label.slice(1)}`, tone: w.tone };
}

/** "Bullish", "Neutral", "Bearish", or why there is no reading, for a TA pill. */
export function taPillText(frame: TaFrameView): string {
  if (frame.status === "missing") return "No reading";
  if (frame.status === "error") return "Couldn't load";
  if (frame.status === "not_loaded") return "Loading";
  return frame.lean === "bullish" ? "Bullish" : frame.lean === "bearish" ? "Bearish" : frame.lean === "neutral" ? "Neutral" : "Unknown";
}
