/**
 * Alert setup chart windows (#299, #298). Pure functions, no I/O, so tests load them directly.
 *
 * A window is copied at event time because the operational candle store keeps only ~8 days. It
 * holds only completed candles up to and including the event candle (point-in-time; no "what
 * happened next"), in a compact form: candles as [open_ms, open, high, low, close, volume].
 * The machine-side renderer (python/market_analysis_tools/alert_charts.py) draws the PNG from it.
 */

export const ALERT_CHART_WINDOW_VERSION = "alert-chart-window-v1";
/** process_cumulative_observation: change from the saved baseline >= threshold, directional cooldown. */
export const MOVEMENT_RULE_VERSION = "cumulative-baseline-v1";
export const MOVEMENT_WINDOW_TIMEFRAME = 15;
/** 24 h of completed 15m candles ending at the last 15m candle completed by the alert time. */
export const MOVEMENT_WINDOW_CANDLES = 96;
export const PATTERN_WINDOW_CANDLES = 60;
/** The breadth reading is attached only when its evaluation boundary is this close to the alert. */
export const BREADTH_MAX_DISTANCE_MS = 120_000;

export type ChartCandle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type WindowCandle = [number, number, number, number, number, number];

export type MovementBreadth = {
  status: string;
  window_minutes: number;
  evaluation_boundary_ms: number;
  direction_state: string;
  denominator: number;
  rising: number | null;
  falling: number | null;
  flat: number | null;
  material_rising: number | null;
  material_falling: number | null;
};

export type MovementAlertInput = {
  alertId: string;
  userId: string;
  symbol: string;
  source: string;
  direction: "up" | "down";
  thresholdPct: number;
  changePct: number;
  baselinePrice: number;
  baselineAt: string;
  alertPrice: number;
  /** Close time of the completed 1m observation (process_cumulative_observation p_observed_at). */
  observedAt: string;
  /** The completed 1m candle that produced the observation, when the caller has it. */
  alertMinute: ChartCandle | null;
  candles15: ChartCandle[];
  breadth: MovementBreadth | null;
  breadthUnavailableReason: string | null;
};

export type PatternEventInput = {
  eventId: string;
  symbol: string;
  source: string;
  timeframe: number;
  /** Open time of the event candle (ta_signals.candle_at). */
  eventCandleAt: number;
  patterns: string[];
  ruleVersion: string;
  /** Completed candles of the event timeframe, oldest first; may extend past the event candle. */
  candles: ChartCandle[];
};

export type ChartCapture = {
  eventKind: "movement_alert" | "pattern";
  eventId: string;
  userId: string | null;
  symbol: string;
  timeframeMinutes: number;
  eventCandleAt: string;
  ruleVersion: string;
  scopeKey: string;
  windowVersion: string;
  window: Record<string, unknown>;
};

const MINUTE = 60_000;

function finite(value: number): boolean {
  return typeof value === "number" && Number.isFinite(value);
}

function checkCandles(candles: ChartCandle[], timeframe: number): ChartCandle[] {
  const sorted = [...candles].sort((a, b) => a.time - b.time);
  for (let index = 0; index < sorted.length; index += 1) {
    const candle = sorted[index]!;
    if (
      !Number.isSafeInteger(candle.time) || candle.time % (timeframe * MINUTE) !== 0 ||
      ![candle.open, candle.high, candle.low, candle.close, candle.volume].every(finite) ||
      candle.high < Math.max(candle.open, candle.close) || candle.low > Math.min(candle.open, candle.close) ||
      candle.low <= 0 || candle.volume < 0 ||
      (index > 0 && sorted[index - 1]!.time === candle.time)
    ) {
      throw new Error("Invalid or duplicate chart candle");
    }
  }
  return sorted;
}

function compact(candle: ChartCandle): WindowCandle {
  return [candle.time, candle.open, candle.high, candle.low, candle.close, candle.volume];
}

/**
 * Completed candles that open at or before the event candle, the event candle last. Throws when
 * the event candle itself is missing, so a window never silently ends earlier than the event.
 */
export function pointInTimeCandles(
  candles: ChartCandle[], eventCandleAt: number, timeframe: number,
): ChartCandle[] {
  const history = checkCandles(candles, timeframe).filter((candle) => candle.time <= eventCandleAt);
  if (history.at(-1)?.time !== eventCandleAt) throw new Error("Event candle is not in the chart history");
  return history;
}

/** technical.py `_ema`: SMA seed over the first `period` values, then the standard recursion. */
export function emaSeries(values: number[], period: number): (number | null)[] {
  const output: (number | null)[] = values.map(() => null);
  if (values.length < period) return output;
  let total = 0;
  for (const value of values.slice(0, period)) total += value;
  let previous = total / period;
  output[period - 1] = previous;
  const exponent = 2 / (period + 1);
  for (let index = period; index < values.length; index += 1) {
    previous = (values[index]! - previous) * exponent + previous;
    output[index] = previous;
  }
  return output;
}

/**
 * technical.py `_rsi` (Wilder averages, rounded to 2 decimals), aligned to `values`: the first
 * value lands on index `period` (the candle after the first `period` changes).
 */
export function rsiSeries(values: number[], period = 14): (number | null)[] {
  const output: (number | null)[] = values.map(() => null);
  if (values.length <= period) return output;
  const gains: number[] = [];
  const losses: number[] = [];
  for (let index = 1; index < values.length; index += 1) {
    const change = values[index]! - values[index - 1]!;
    gains.push(change > 0 ? change : 0);
    losses.push(change < 0 ? -change : 0);
  }
  let averageGain = gains.slice(0, period).reduce((a, b) => a + b, 0) / period;
  let averageLoss = losses.slice(0, period).reduce((a, b) => a + b, 0) / period;
  for (let index = period - 1; index < gains.length; index += 1) {
    if (index >= period) {
      averageGain = (averageGain * (period - 1) + gains[index]!) / period;
      averageLoss = (averageLoss * (period - 1) + losses[index]!) / period;
    }
    let value: number;
    if (averageLoss === 0) value = 100;
    else if (averageGain === 0) value = 0;
    else value = pythonRound2(100 - 100 / (1 + averageGain / averageLoss));
    output[index + 1] = value;
  }
  return output;
}

/** Python's round(x, 2) (round-half-even on the binary value), not Math.round's half-up. */
function pythonRound2(value: number): number {
  const scaled = value * 100;
  const floor = Math.floor(scaled);
  const diff = scaled - floor;
  const rounded = diff > 0.5 ? floor + 1 : diff < 0.5 ? floor : floor % 2 === 0 ? floor : floor + 1;
  return rounded / 100;
}

function roundSeries(values: (number | null)[]): (number | null)[] {
  return values.map((value) => (value === null ? null : Number(value.toPrecision(12))));
}

export function buildPatternCapture(input: PatternEventInput): ChartCapture {
  if (!input.patterns.length) throw new Error("A pattern chart needs at least one pattern");
  if (![15, 60, 240].includes(input.timeframe)) throw new Error("Unsupported pattern timeframe");
  const history = pointInTimeCandles(input.candles, input.eventCandleAt, input.timeframe);
  // Indicators over the whole point-in-time history (the same prefix the TA request used), so the
  // window needs no warm-up candles; then only the last PATTERN_WINDOW_CANDLES are kept.
  const closes = history.map((candle) => candle.close);
  const keep = Math.min(PATTERN_WINDOW_CANDLES, history.length);
  const tail = <T>(series: T[]) => series.slice(series.length - keep);
  const window = {
    version: ALERT_CHART_WINDOW_VERSION,
    kind: "pattern",
    symbol: input.symbol,
    source: input.source,
    timeframe_minutes: input.timeframe,
    event_candle_ms: input.eventCandleAt,
    patterns: [...input.patterns].sort(),
    rule_version: input.ruleVersion,
    candles: tail(history).map(compact),
    expected_candles: PATTERN_WINDOW_CANDLES,
    indicators: {
      ema20: roundSeries(tail(emaSeries(closes, 20))),
      ema50: roundSeries(tail(emaSeries(closes, 50))),
      rsi14: tail(rsiSeries(closes, 14)),
      history_candles: history.length,
    },
  };
  return {
    eventKind: "pattern",
    eventId: input.eventId,
    userId: null,
    symbol: input.symbol,
    timeframeMinutes: input.timeframe,
    eventCandleAt: new Date(input.eventCandleAt).toISOString(),
    ruleVersion: input.ruleVersion,
    scopeKey: "",
    windowVersion: ALERT_CHART_WINDOW_VERSION,
    window,
  };
}

export function buildMovementCapture(input: MovementAlertInput): ChartCapture {
  const observedMs = Date.parse(input.observedAt);
  if (!Number.isSafeInteger(observedMs) || observedMs % MINUTE !== 0) throw new Error("Invalid alert observation time");
  const alertCandleAt = observedMs - MINUTE;
  if (input.alertMinute && input.alertMinute.time !== alertCandleAt) {
    throw new Error("Alert minute candle does not match the observation");
  }
  const duration = MOVEMENT_WINDOW_TIMEFRAME * MINUTE;
  // Completed by the alert time: a 15m candle is in the window only when it closed at or before the
  // observation, so the developing 15m candle that contains the alert minute is never included.
  const completed = checkCandles(input.candles15, MOVEMENT_WINDOW_TIMEFRAME)
    .filter((candle) => candle.time + duration <= observedMs);
  if (!completed.length) throw new Error("No completed 15m candle before the alert");
  const candles = completed.slice(-MOVEMENT_WINDOW_CANDLES);
  // Indicators over every completed candle read (more than the window when available), so EMA 50
  // is warmed up before the window starts.
  const closes = completed.map((candle) => candle.close);
  const tail = <T>(series: T[]) => series.slice(series.length - candles.length);
  const window = {
    version: ALERT_CHART_WINDOW_VERSION,
    kind: "movement_alert",
    symbol: input.symbol,
    source: input.source,
    timeframe_minutes: MOVEMENT_WINDOW_TIMEFRAME,
    event_candle_ms: candles.at(-1)!.time,
    candles: candles.map(compact),
    expected_candles: MOVEMENT_WINDOW_CANDLES,
    indicators: {
      ema20: roundSeries(tail(emaSeries(closes, 20))),
      ema50: roundSeries(tail(emaSeries(closes, 50))),
      rsi14: tail(rsiSeries(closes, 14)),
      history_candles: completed.length,
    },
    alert: {
      alert_id: input.alertId,
      rule_version: MOVEMENT_RULE_VERSION,
      direction: input.direction,
      threshold_pct: input.thresholdPct,
      change_pct: input.changePct,
      baseline_price: input.baselinePrice,
      baseline_ms: Date.parse(input.baselineAt),
      alert_price: input.alertPrice,
      alert_ms: observedMs,
      alert_candle_ms: alertCandleAt,
      alert_minute: input.alertMinute ? compact(input.alertMinute) : null,
    },
    breadth: input.breadth,
    breadth_unavailable_reason: input.breadth ? null : input.breadthUnavailableReason ?? "not_available",
  };
  return {
    eventKind: "movement_alert",
    eventId: input.alertId,
    userId: input.userId,
    symbol: input.symbol,
    timeframeMinutes: 1,
    eventCandleAt: new Date(alertCandleAt).toISOString(),
    ruleVersion: MOVEMENT_RULE_VERSION,
    scopeKey: input.userId,
    windowVersion: ALERT_CHART_WINDOW_VERSION,
    window,
  };
}

/** The idempotency identity, as the operational unique key sees it. */
export function captureIdentity(capture: ChartCapture): string {
  return [capture.eventKind, capture.symbol, capture.timeframeMinutes, capture.eventCandleAt,
    capture.ruleVersion, capture.scopeKey].join("|");
}

type BreadthSideMetric = { available: boolean; value: { count: number } | null } | undefined;

/**
 * The primary-window breadth of the movement engine's current state, only when that state is LIVE
 * and its evaluation boundary lies within BREADTH_MAX_DISTANCE_MS of the alert. The engine keeps
 * no breadth history, so a reading from another time is reported as unavailable, never borrowed.
 */
export function breadthAtAlert(
  state: {
    status: string;
    primary: {
      windowMinutes: number;
      directionState: string;
      evidence: {
        evaluationBoundaryTime: number;
        breadth: {
          denominator: number;
          rising: BreadthSideMetric; falling: BreadthSideMetric; flat: BreadthSideMetric;
          materialRising: BreadthSideMetric; materialFalling: BreadthSideMetric;
        };
      };
    } | null;
  } | null,
  observedMs: number,
): { breadth: MovementBreadth | null; reason: string | null } {
  if (!state || !state.primary) return { breadth: null, reason: "movement_state_unavailable" };
  if (state.status !== "LIVE") return { breadth: null, reason: `movement_state_${state.status.toLowerCase()}` };
  const boundary = state.primary.evidence.evaluationBoundaryTime;
  if (!Number.isFinite(boundary) || Math.abs(boundary - observedMs) > BREADTH_MAX_DISTANCE_MS) {
    return { breadth: null, reason: "movement_state_not_at_alert_time" };
  }
  const b = state.primary.evidence.breadth;
  const count = (metric: BreadthSideMetric) => (metric?.available && metric.value ? metric.value.count : null);
  return {
    breadth: {
      status: state.status,
      window_minutes: state.primary.windowMinutes,
      evaluation_boundary_ms: boundary,
      direction_state: state.primary.directionState,
      denominator: b.denominator,
      rising: count(b.rising),
      falling: count(b.falling),
      flat: count(b.flat),
      material_rising: count(b.materialRising),
      material_falling: count(b.materialFalling),
    },
    reason: null,
  };
}
