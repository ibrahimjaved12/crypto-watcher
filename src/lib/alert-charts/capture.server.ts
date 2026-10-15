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
import { getAlertChartStore, type AlertChartStore } from "./store.server";

type AdminClient = Pick<SupabaseClient<Database>, "from" | "rpc">;

export type CaptureOutcome = { status: "captured" | "duplicate" | "skipped" | "failed"; error?: string };

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

export type MovementCaptureDeps = {
  db: AdminClient;
  operationalStore: OperationalStore;
  context: MonitorRunContext;
  userId: string;
  symbol: string;
  alertId: string;
  /** 1m candles of the observation that produced the alert. */
  minute: Candle[];
  /** Collector mode: read 15m candles from the operational store instead of the exchange. */
  collectorOwnsMarketData: boolean;
  store?: AlertChartStore | null;
  config?: AlertChartsConfig;
};

export async function captureMovementAlertChart(deps: MovementCaptureDeps): Promise<CaptureOutcome> {
  try {
    const config = deps.config ?? alertChartsConfig();
    const store = resolveStore(deps.store, config);
    if (!store) return { status: "skipped" };
    const { data: alert, error } = await deps.db
      .from("alerts")
      .select("id, symbol, price, change_pct, threshold_pct, baseline_price, baseline_at, observed_at, data_source, rule")
      .eq("id", deps.alertId)
      .eq("user_id", deps.userId)
      .single();
    if (error || !alert) throw new Error(`alert read failed: ${error?.message ?? "not found"}`);
    if (alert.baseline_price === null || !alert.baseline_at || !alert.observed_at) {
      throw new Error("alert has no baseline or observation time");
    }
    const observedMs = Date.parse(alert.observed_at);
    let candles15: Candle[];
    if (deps.collectorOwnsMarketData) {
      const history = await deps.operationalStore.readCollectorCompletedCandles(
        // The 260 newest completed 15m candles: the window plus indicator warm-up.
        deps.symbol, MOVEMENT_WINDOW_TIMEFRAME, Math.max(260, MOVEMENT_WINDOW_CANDLES),
      );
      candles15 = history.observations.map(({ candle }) => ({
        time: candle.openTime, open: candle.open, high: candle.high, low: candle.low, close: candle.close,
        volume: candle.baseVolume, complete: true,
      }));
    } else {
      candles15 = (await deps.context.ta(deps.symbol, MOVEMENT_WINDOW_TIMEFRAME, () => {})).candles
        .filter((candle) => candle.complete !== false);
    }
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
    const saved = await store.enqueue(capture);
    return { status: saved.created ? "captured" : "duplicate" };
  } catch (error) {
    const text = message(error);
    console.warn(`[alert-charts] movement alert chart not captured for ${deps.symbol}: ${text}`);
    return { status: "failed", error: text };
  }
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
      outcomes.push({ status: "failed", error: text });
    }
  }
  return outcomes;
}
