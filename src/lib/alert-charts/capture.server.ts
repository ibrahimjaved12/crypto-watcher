/**
 * Event-time capture of alert setup chart windows (#299). Called after a movement alert is saved and
 * after TA pattern rows are written. Best effort by design: a capture failure is logged and returned,
 * never thrown, so it cannot fail or roll back the alert or the TA signal it describes.
 */
import type { SupabaseClient } from "@supabase/supabase-js";

import type { Database } from "@/integrations/supabase/types";
import type { Candle } from "../market/providers.server";
import { MARKET_UNIVERSE_ID, toMarketMovementCurrentState } from "../market/market-movement-state";
import type { MonitorRunContext } from "../monitor/run-context";
import type { OperationalStore } from "../operational/types";
import { alertChartsConfig, type AlertChartsConfig } from "./config";
import {
  MOVEMENT_WINDOW_CANDLES,
  MOVEMENT_WINDOW_TIMEFRAME,
  breadthAtAlert,
  buildMovementCapture,
  buildPatternCapture,
  type ChartCandle,
} from "./capture";
import { getAlertChartStore, type AlertChartStore, type CaptureFailure } from "./store.server";

type AdminClient = Pick<SupabaseClient<Database>, "from" | "rpc">;

export type CaptureOutcome = { status: "captured" | "duplicate" | "skipped" | "failed"; error?: string };

/** One retry of a transient read, after this delay (the retry is only for read steps). */
export const READ_RETRY_DELAY_MS = 2_000;
/** Hard cap on how long the end of a monitor run waits for deferred movement captures. */
export const CAPTURE_SETTLE_CAP_MS = 10_000;

function message(error: unknown): string {
  return (error instanceof Error ? error.message : String(error)).replace(/\s+/g, " ").slice(0, 300);
}

function resolveStore(store: AlertChartStore | null | undefined, config: AlertChartsConfig) {
  if (!config.captureEnabled) return null;
  return store === undefined ? getAlertChartStore(config) : store;
}

function chartCandle(candle: Candle): ChartCandle {
  return { time: candle.time, open: candle.open, high: candle.high, low: candle.low, close: candle.close,
    volume: candle.volume };
}

/** Best effort: a failure to record a failure is logged and never thrown. */
async function recordFailure(store: AlertChartStore | null, failure: () => CaptureFailure): Promise<void> {
  if (!store) return;
  try {
    await store.recordFailure(failure());
  } catch (error) {
    console.warn(`[alert-charts] could not record a capture failure: ${message(error)}`);
  }
}

/** Run a read step; on a throw wait `delayMs` and run it once more. Validation errors are outside this wrapper. */
export async function readWithRetry<T>(
  read: () => Promise<T>, delayMs: number, canRetry: () => boolean = () => true,
): Promise<T> {
  try {
    return await read();
  } catch (error) {
    if (!canRetry()) throw error; // e.g. the capture was abandoned: do not wait or read again
    if (delayMs > 0) await new Promise((done) => setTimeout(done, delayMs));
    return read();
  }
}

/** Shared flag of one deferred capture; once set, the capture neither reads further nor writes a chart. */
export type CaptureControl = { abandoned: boolean };

export type MovementCaptureDeps = {
  db: AdminClient;
  operationalStore: OperationalStore;
  context: MonitorRunContext;
  userId: string;
  symbol: string;
  alertId: string;
  /** Close time of the completed 1m observation that produced the alert (ISO). */
  observedAt: string;
  /** 1m candles of the observation that produced the alert. */
  minute: Candle[];
  /** Collector mode: read 15m candles from the operational store instead of the exchange. */
  collectorOwnsMarketData: boolean;
  store?: AlertChartStore | null;
  config?: AlertChartsConfig;
  retryDelayMs?: number;
  control?: CaptureControl;
};

function movementFailure(deps: MovementCaptureDeps, error: string): CaptureFailure {
  return { eventKind: "movement_alert", eventId: deps.alertId, symbol: deps.symbol, timeframeMinutes: 1,
    eventCandleAt: new Date(Date.parse(deps.observedAt) - 60_000).toISOString(), error };
}

export async function captureMovementAlertChart(deps: MovementCaptureDeps): Promise<CaptureOutcome> {
  let store: AlertChartStore | null = null;
  try {
    const config = deps.config ?? alertChartsConfig();
    store = resolveStore(deps.store, config);
    if (!store) return { status: "skipped" };
    const delay = deps.retryDelayMs ?? READ_RETRY_DELAY_MS;
    const stopped = () => {
      if (deps.control?.abandoned) throw new Error("capture abandoned after the time cap");
    };
    const alert = await readWithRetry(async () => {
      stopped();
      const { data, error } = await deps.db
        .from("alerts")
        .select("id, symbol, price, change_pct, threshold_pct, baseline_price, baseline_at, observed_at, data_source, rule")
        .eq("id", deps.alertId)
        .eq("user_id", deps.userId)
        .single();
      if (error || !data) throw new Error(`alert read failed: ${error?.message ?? "not found"}`);
      return data;
    }, delay, () => !deps.control?.abandoned);
    if (alert.baseline_price === null || !alert.baseline_at || !alert.observed_at) {
      throw new Error("alert has no baseline or observation time"); // not transient: never retried
    }
    const observedMs = Date.parse(alert.observed_at);
    const candles15: Candle[] = await readWithRetry(async () => {
      stopped();
      if (deps.collectorOwnsMarketData) {
        const history = await deps.operationalStore.readCollectorCompletedCandles(
          // The 260 newest completed 15m candles: the window plus indicator warm-up.
          deps.symbol, MOVEMENT_WINDOW_TIMEFRAME, Math.max(260, MOVEMENT_WINDOW_CANDLES),
        );
        return history.observations.map(({ candle }) => ({
          time: candle.openTime, open: candle.open, high: candle.high, low: candle.low, close: candle.close,
          volume: candle.baseVolume, complete: true,
        }));
      }
      return (await deps.context.ta(deps.symbol, MOVEMENT_WINDOW_TIMEFRAME, () => {})).candles
        .filter((candle) => candle.complete !== false);
    }, delay, () => !deps.control?.abandoned);
    const alertMinute = deps.minute.find((candle) => candle.time === observedMs - 60_000) ?? null;
    let breadth: ReturnType<typeof breadthAtAlert> = { breadth: null, reason: "operational_store_disabled" };
    if (deps.operationalStore.enabled) {
      const current = await deps.operationalStore.getMarketStateCurrent(MARKET_UNIVERSE_ID).catch(() => null);
      breadth = breadthAtAlert(toMarketMovementCurrentState(current, { now: Date.now() }), observedMs);
    }
    const capture = buildMovementCapture({
      alertId: alert.id,
      userId: deps.userId,
      symbol: deps.symbol,
      source: alert.data_source,
      direction: Number(alert.change_pct) >= 0 ? "up" : "down",
      thresholdPct: Number(alert.threshold_pct),
      changePct: Number(alert.change_pct),
      baselinePrice: Number(alert.baseline_price),
      baselineAt: alert.baseline_at,
      alertPrice: Number(alert.price),
      observedAt: alert.observed_at,
      alertMinute: alertMinute ? chartCandle(alertMinute) : null,
      candles15: candles15.map(chartCandle),
      breadth: breadth.breadth,
      breadthUnavailableReason: breadth.reason,
    });
    stopped();
    const saved = await store.enqueue(capture);
    // Hosted copy (#298): show the alert's chart as "waiting for the machine" on the Alerts page. Only when this
    // machine is configured to upload; best effort (the upload protocol creates the row anyway).
    if (config.hosted) {
      try {
        await deps.db.rpc("register_alert_chart_pending", { p_alert_id: alert.id });
      } catch (error) {
        console.warn(`[alert-charts] hosted chart row not registered for ${deps.symbol}: ${message(error)}`);
      }
    }
    return { status: saved.created ? "captured" : "duplicate" };
  } catch (error) {
    const text = message(error);
    console.warn(`[alert-charts] movement alert chart not captured for ${deps.symbol}: ${text}`);
    await recordFailure(store, () => movementFailure(deps, text));
    return { status: "failed", error: text };
  }
}

/** A movement capture started without awaiting it (the next symbols' alerts are not delayed). */
export type DeferredCapture = {
  symbol: string;
  alertId: string;
  control: CaptureControl;
  /** Never rejects. */
  promise: Promise<CaptureOutcome>;
  failure: () => CaptureFailure;
  store: AlertChartStore | null | undefined;
};

export function startMovementCapture(deps: MovementCaptureDeps): DeferredCapture {
  const control: CaptureControl = deps.control ?? { abandoned: false };
  const promise = captureMovementAlertChart({ ...deps, control }).catch(
    (error): CaptureOutcome => ({ status: "failed", error: message(error) }),
  );
  return { symbol: deps.symbol, alertId: deps.alertId, control, promise, store: deps.store,
    failure: () => movementFailure(deps, "") };
}

/**
 * Await the deferred captures of a run (after every alert is saved) for at most `capMs`. Captures still running
 * at the cap are abandoned (they will neither read nor write a chart any more), logged by symbol and recorded as
 * capture failures. Never throws.
 */
export async function settleMovementCaptures(
  pending: DeferredCapture[], options: { capMs?: number; config?: AlertChartsConfig } = {},
): Promise<{ settled: number; abandoned: string[] }> {
  if (pending.length === 0) return { settled: 0, abandoned: [] };
  const capMs = options.capMs ?? CAPTURE_SETTLE_CAP_MS;
  const open = new Set(pending);
  let timer: ReturnType<typeof setTimeout> | undefined;
  const tracked = pending.map((item) => item.promise.then(() => { open.delete(item); }));
  const cap = new Promise<void>((done) => { timer = setTimeout(done, capMs); });
  try {
    await Promise.race([Promise.allSettled(tracked), cap]);
  } finally {
    clearTimeout(timer);
  }
  const abandoned = [...open];
  for (const item of abandoned) item.control.abandoned = true;
  if (abandoned.length) {
    console.warn(`[alert-charts] abandoned ${abandoned.length} movement capture(s) after ${capMs} ms: `
      + abandoned.map((item) => item.symbol).join(", "));
    let store: AlertChartStore | null = null;
    try {
      store = resolveStore(abandoned[0]!.store, options.config ?? alertChartsConfig());
    } catch { /* no store: the log line above is all there is */ }
    for (const item of abandoned) {
      await recordFailure(store, () => ({ ...item.failure(), error: `capture abandoned after ${capMs} ms` }));
    }
  }
  return { settled: pending.length - abandoned.length, abandoned: abandoned.map((item) => item.symbol) };
}

export type PatternCaptureDeps = {
  symbol: string;
  source: string;
  timeframe: number;
  ruleVersion: string;
  /** Completed candles of the timeframe the TA run used (may extend past the event candle). */
  candles: Candle[];
  events: Array<{ id: string; candleAt: number; patterns: string[] }>;
  store?: AlertChartStore | null;
  config?: AlertChartsConfig;
};

export async function capturePatternCharts(deps: PatternCaptureDeps): Promise<CaptureOutcome[]> {
  let config: AlertChartsConfig;
  let store: AlertChartStore | null;
  try {
    config = deps.config ?? alertChartsConfig();
    store = resolveStore(deps.store, config);
  } catch (error) {
    console.warn(`[alert-charts] pattern charts disabled: ${message(error)}`);
    return deps.events.map((): CaptureOutcome => ({ status: "failed", error: message(error) }));
  }
  const outcomes: CaptureOutcome[] = [];
  for (const event of deps.events) {
    if (!store || event.patterns.length === 0) {
      outcomes.push({ status: "skipped" });
      continue;
    }
    try {
      // The candles are already in memory (no read step), so there is nothing transient to retry.
      const capture = buildPatternCapture({
        eventId: event.id, symbol: deps.symbol, source: deps.source, timeframe: deps.timeframe,
        eventCandleAt: event.candleAt, patterns: event.patterns, ruleVersion: deps.ruleVersion,
        candles: deps.candles.map(chartCandle),
      });
      const saved = await store.enqueue(capture);
      outcomes.push({ status: saved.created ? "captured" : "duplicate" });
    } catch (error) {
      const text = message(error);
      console.warn(`[alert-charts] pattern chart not captured for ${deps.symbol} ${deps.timeframe}m: ${text}`);
      await recordFailure(store, () => ({ eventKind: "pattern", eventId: event.id, symbol: deps.symbol,
        timeframeMinutes: deps.timeframe, eventCandleAt: new Date(event.candleAt).toISOString(), error: text }));
      outcomes.push({ status: "failed", error: text });
    }
  }
  return outcomes;
}
