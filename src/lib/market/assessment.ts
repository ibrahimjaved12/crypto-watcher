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
  type MarketMovementCurrentState,
  type MovementEngineStatus,
} from "./market-movement-state";
import { DEFAULT_MARKET_MOVEMENT_CONFIG, type Metric } from "./movement-metrics-contract";
import { QUOTE_STALE_MS } from "./quote-calc";
import { humanizeToken } from "../labels";

export const ASSESSMENT_WINDOWS = [1, 5, 15] as const;
export const TA_ASSESSMENT_FRAMES = [15, 60, 240] as const;
/**
 * A TA snapshot is stale when its candle closed more than one timeframe plus this allowance ago: by then the
 * next candle has closed and the scheduled TA run (every few minutes) should have saved a newer snapshot.
 */
export const TA_RUN_ALLOWANCE_MS = 10 * 60_000;

/** Whether the page could read the engine state at all; separate from the engine's own status. */
export type MovementReadStatus = "ok" | "store_disabled" | "read_failed" | "not_loaded";

export type StatusTone = "live" | "warming" | "stale" | "unavailable";

// ---------------------------------------------------------------- shared helpers

export function formatAge(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 90) return `${seconds} s`;
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
  /** What "material" means, with the engine's value when it is known for the saved config. */
  materialNote: string;
  medianMovePct: number | null;
  medianNormalized: number | null;
  dispersion: number | null;
  outliers: Array<{ symbol: string; direction: "rising" | "falling"; movePct: number; crossSectionalZ: number }>;
};

export type MarketStripView = {
  status: MovementEngineStatus;
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

export function buildMarketStrip(
  state: MarketMovementCurrentState,
  now: number,
  read: MovementReadStatus = "ok",
): MarketStripView {
  const boundary = state.timestamps?.evaluationBoundaryTime ?? null;
  // The status is the engine's; a read failure or a disabled store always shows as unavailable.
  const status: MovementEngineStatus = read === "ok" ? state.status : "UNAVAILABLE";
  const age = boundary === null ? null : formatAge(now - boundary);
  const base = {
    status,
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

  if (status === "UNAVAILABLE") {
    return { ...base, tone: "unavailable", statusLabel: "Unavailable", current: false, explanation: unavailableReason(state, read),
      direction: null, pace: null, windows: [], universe };
  }
  if (status === "STALE") {
    return { ...base, tone: "stale", statusLabel: "Stale", current: false,
      explanation: `Not live: the last evaluation is ${age} old (older than ${formatAge(MOVEMENT_ENGINE_STALE_AFTER_MS)}). The numbers below are as of then.`,
      direction, pace, windows, universe };
  }
  if (status === "WARMING") {
    return { ...base, tone: "warming", statusLabel: "Warming up", current: false,
      explanation: "The engine is collecting live 5-second history; the market-wide state appears once there is enough.",
      direction, pace, windows, universe };
  }
  return { ...base, tone: "live", statusLabel: "Live", current: true,
    explanation: `Evaluated ${age} ago on ${universe?.included.length ?? 0} of ${universe?.configured.length ?? 0} watched pairs.`,
    direction, pace, windows, universe };
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
    };

function zLabel(z: number, thresholds: ReturnType<typeof engineThresholds>, material: boolean): { label: string; tone: PairWindowView["tone"] } {
  const direction = z > 0 ? "up" : "down";
  if (!thresholds) return { label: `${z > 0 ? "above" : "below"} its normal (config unknown)`, tone: direction };
  const size = Math.abs(z);
  if (size < thresholds.flatZ) return { label: "about normal", tone: "flat" };
  if (material || size >= thresholds.materialZ) return { label: z > 0 ? "unusually strong rise" : "unusually sharp drop", tone: direction };
  return { label: z > 0 ? "a little stronger than usual" : "a little weaker than usual", tone: direction };
}

export function buildPairMovement(symbol: string, state: MarketMovementCurrentState, read: MovementReadStatus = "ok"): PairMovementView {
  if (read !== "ok" || !state.available) return { kind: "unavailable", reason: unavailableReason(state, read) };
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
  return { kind: "ok", windows, rvol, rvolReason, outlier, partialReasons: [...partial] };
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
  const stale = behind > timeframe * 60_000 + TA_RUN_ALLOWANCE_MS;
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
      ? `Stale: the candle closed ${formatAge(behind)} ago, so a newer candle exists that has no snapshot yet.`
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
  status: "fresh" | "stale" | "unavailable";
  detail: string;
  asOf: number | null;
};

export type FreshnessInput = {
  state: MarketMovementCurrentState;
  read: MovementReadStatus;
  symbols: readonly string[];
  quotes: ReadonlyArray<{ symbol: string; ok: boolean; stale: boolean; lastCandleAt: number | null }>;
  /** Latest TA rows (any order); undefined while not loaded. */
  ta: readonly TaRowInput[] | undefined;
};

/** Four separate source lines. A fresh page render never hides one of them being old. */
export function buildFreshness(input: FreshnessInput, now: number): FreshnessItem[] {
  const { state, read, symbols, quotes, ta } = input;
  const items: FreshnessItem[] = [];
  const engineLimit = formatAge(MOVEMENT_ENGINE_STALE_AFTER_MS);

  // Market stream: the engine's per-symbol source-time evidence at its last evaluation.
  const primary = state.context.find((w) => w.windowMinutes === 5);
  if (read !== "ok" || !primary) {
    items.push({ id: "stream", label: "Market stream", status: "unavailable",
      detail: read === "store_disabled" ? "Not readable from this site." : "No stream evidence from the engine.", asOf: null });
  } else {
    const evidence = primary.sourceTimeEvidence ?? [];
    const watched = symbols.length ? symbols : evidence.map((e) => e.symbol);
    const lagging: string[] = [];
    let newest: number | null = null;
    for (const symbol of watched) {
      const at = evidence.find((e) => e.symbol === symbol)?.lastReceivedAtMs ?? null;
      if (at === null || now - at > MOVEMENT_ENGINE_STALE_AFTER_MS) lagging.push(symbol.replace(/USDT$/, ""));
      if (at !== null && (newest === null || at > newest)) newest = at;
    }
    items.push({ id: "stream", label: "Market stream", asOf: newest,
      status: newest === null || lagging.length > 0 ? "stale" : "fresh",
      detail: lagging.length
        ? `No trade data within ${engineLimit} for ${lagging.join(", ")}${newest === null ? "" : `; newest ${formatAge(now - newest)} ago`}.`
        : `Every watched pair received data within ${engineLimit} (newest ${formatAge(now - (newest ?? now))} ago).` });
  }

  // Movement evaluation.
  const boundary = state.timestamps?.evaluationBoundaryTime ?? null;
  if (read !== "ok" || boundary === null) {
    items.push({ id: "movement", label: "Movement evaluation", status: "unavailable",
      detail: read === "store_disabled" ? "Not readable from this site." : "No evaluation saved yet.", asOf: null });
  } else {
    const old = now - boundary > MOVEMENT_ENGINE_STALE_AFTER_MS;
    items.push({ id: "movement", label: "Movement evaluation", asOf: boundary, status: old ? "stale" : "fresh",
      detail: `Last evaluation ${formatAge(now - boundary)} ago (stale after ${engineLimit}); engine status ${state.status.toLowerCase()}.` });
  }

  // Completed candles behind the price cards.
  const candleQuotes = quotes.filter((q) => symbols.length === 0 || symbols.includes(q.symbol));
  if (candleQuotes.length === 0) {
    items.push({ id: "candles", label: "Completed candles", status: "unavailable", detail: "No quotes loaded yet.", asOf: null });
  } else {
    const stale = candleQuotes.filter((q) => !q.ok || q.stale || q.lastCandleAt === null || now - q.lastCandleAt > QUOTE_STALE_MS);
    const times = candleQuotes.flatMap((q) => (q.lastCandleAt === null ? [] : [q.lastCandleAt]));
    const oldest = times.length ? Math.min(...times) : null;
    items.push({ id: "candles", label: "Completed candles", asOf: oldest, status: stale.length ? "stale" : "fresh",
      detail: stale.length
        ? `Old or missing for ${stale.map((q) => q.symbol.replace(/USDT$/, "")).join(", ")} (older than ${formatAge(QUOTE_STALE_MS)}).`
        : `Newest candle at most ${oldest === null ? "?" : formatAge(now - oldest)} old on every pair.` });
  }

  // TA snapshots.
  if (ta === undefined) {
    items.push({ id: "ta", label: "Technical analysis", status: "unavailable", detail: "Not loaded yet.", asOf: null });
  } else {
    const latest = latestTaBySymbol(ta);
    const stale: string[] = [];
    const missing: string[] = [];
    let oldestClose: number | null = null;
    for (const symbol of symbols) {
      for (const timeframe of TA_ASSESSMENT_FRAMES) {
        const row = latest.get(`${symbol}|${timeframe}`);
        const view = buildTaFrame(row, timeframe, now);
        const name = `${symbol.replace(/USDT$/, "")} ${view.label}`;
        if (view.status === "missing") missing.push(name);
        else if (view.status === "stale") stale.push(name);
        if (view.candleClosedAt !== null && (oldestClose === null || view.candleClosedAt < oldestClose)) oldestClose = view.candleClosedAt;
      }
    }
    const problems = [
      ...(stale.length ? [`stale: ${stale.join(", ")}`] : []),
      ...(missing.length ? [`no snapshot: ${missing.join(", ")}`] : []),
    ];
    items.push({ id: "ta", label: "Technical analysis", asOf: oldestClose,
      status: problems.length ? (symbols.length && missing.length === symbols.length * TA_ASSESSMENT_FRAMES.length ? "unavailable" : "stale") : "fresh",
      detail: problems.length ? problems.join("; ") : "Every pair has a current snapshot for 15m, 1h and 4h." });
  }
  return items;
}
