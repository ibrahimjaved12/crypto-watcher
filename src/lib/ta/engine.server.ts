import type { SupabaseClient } from "@supabase/supabase-js";
import { activityEnabled, logActivity } from "../activity-controls";
import type { Database, Json } from "@/integrations/supabase/types";
import { createMonitorRunContext, type MonitorRunContext } from "../monitor/run-context";
import { calculateTechnicalBatch } from "./python-client.server";
import { requestCandles, type TechnicalAnalysisRequest } from "./python-contract";
import { MARKET_SOURCE, type MarketSource } from "../market/symbols";
import type { Candle, FuturesContract, TACandleResult } from "../market/providers.server";
import {
  completedCandles,
  outcomeDue,
  TA_FRAMES,
  TA_INTERPRETATION_VERSION,
  TA_MINIMUM_HISTORY,
  TA_VERSION,
} from "./schedule";
import type { OperationalStore } from "../operational/types";

type Client = Pick<SupabaseClient<Database>, "from" | "rpc">;

type DueWork = {
  work_kind: string;
  id: string;
  timeframe: number;
  candle_at: string;
  source: string;
  detected_at: string;
  price: number;
};

type OutcomePatch = {
  id: string;
  outcome_status: "measured" | "unavailable";
  outcome_at: string | null;
  outcome_price: number | null;
  return_pct: number | null;
};

/**
 * A completed candle with the provenance needed to build a versioned Python request
 * and to persist an immutable conclusion (#24). Collector candles carry the endpoint
 * and actual exchange event time the collector recorded; REST provider candles carry
 * the provider endpoint and no exchange event time. Candle completion is always the
 * deterministic boundary `time + timeframe`.
 */
type MarketCandle = Candle & { endpoint: string; sourceEventTime: number | null };

type MarketHistory = {
  source: MarketSource;
  instrument: Pick<FuturesContract, "id">;
  priceType: string;
  candles: MarketCandle[];
};

/** At most eight missed candles per frame are recovered in one run. */
export const TA_CATCH_UP_LIMIT = 8;

export function latestCompletedCandleAt(timeframe: number, now: number): number {
  const duration = timeframe * 60_000;
  return Math.floor(now / duration) * duration - duration;
}

function sourceNativeSymbol(source: string, symbol: string) {
  const base = symbol.replace(/USDT$/, "");
  if (source === "okx-usdt-swap") return `${base}-USDT-SWAP`;
  if (source === "kraken-futures") return `PF_${base === "BTC" ? "XBT" : base}USD`;
  return symbol;
}

/** Each frame fails independently; movement alerts do not depend on TA. */
export async function runTA(
  db: Client,
  userId: string,
  symbol: string,
  context: MonitorRunContext = createMonitorRunContext(),
  calculate: typeof calculateTechnicalBatch = calculateTechnicalBatch,
  operationalStore: OperationalStore | null | undefined = undefined,
) {
  const generation = activityEnabled(process.env["TA_GENERATION_ENABLED"]);
  const outcomes = activityEnabled(process.env["TA_OUTCOME_EVALUATION_ENABLED"]);
  if (!generation) logActivity(process.env["ACTIVITY_DIAGNOSTICS"], "ta-generation", "skipped");
  if (!outcomes) logActivity(process.env["ACTIVITY_DIAGNOSTICS"], "ta-outcomes", "skipped");
  if (!generation && !outcomes) return [];

  const now = Date.now();
  context.metrics.databaseReads += 1;
  const { data, error: workError } = await db.rpc("get_ta_due_work", {
    p_user_id: userId,
    p_symbol: symbol,
    p_version: TA_VERSION,
    p_now: new Date(now).toISOString(),
    p_include_generation: generation,
    p_include_outcomes: outcomes,
  });
  if (workError) {
    return TA_FRAMES.map((timeframe) => `TA ${symbol} ${timeframe}m: ${workError.message}`);
  }
  const work = (data ?? []) as DueWork[];
  const errors: string[] = [];

  // While the leased collector owns market data, canonical completed candles come
  // from the operational store it writes. The application reads that history
  // instead of fetching a second live exchange TA series; missing or stale history
  // fails visibly rather than falling back to another calculator or data source.
  const collectorStore =
    operationalStore?.enabled && process.env["BINANCE_COLLECTOR_ENABLED"] === "true"
      ? operationalStore
      : null;

  await Promise.all(
    TA_FRAMES.map(async (timeframe) => {
      try {
        const duration = timeframe * 60_000;
        const expected = latestCompletedCandleAt(timeframe, now);
        const latest = work.find(
          (row) => row.work_kind === "latest" && row.timeframe === timeframe,
        );
        const latestAt = latest ? Date.parse(latest.candle_at) : null;
        if (latestAt !== null && (!Number.isFinite(latestAt) || latestAt > expected)) {
          throw new Error("Saved TA checkpoint is invalid or ahead of exchange time");
        }
        const generationDue = generation && (latestAt === null || latestAt < expected);
        const pending = outcomes
          ? work.filter((row) => row.work_kind === "outcome" && row.timeframe === timeframe)
          : [];
        if (!generationDue && pending.length === 0) {
          context.metrics.databaseNoOps += 1;
          return;
        }

        const validate = (candles: Candle[]) => {
          completedCandles(candles, timeframe, now);
        };
        // Collector mode transports the collector's recorded provenance (#24/#26);
        // the REST provider path stays the non-collector source and has no exchange
        // event time. Completion is the deterministic boundary `time + timeframe`.
        const loadMarket = async (
          requestedSource?: string,
        ): Promise<{ market: MarketHistory; provider: TACandleResult | null }> => {
          if (collectorStore) {
            if (requestedSource && requestedSource !== MARKET_SOURCE) {
              throw new Error(
                `Collector owns ${MARKET_SOURCE} candles; ${requestedSource} history is unavailable`,
              );
            }
            const history = await collectorStore.readCollectorCompletedCandles(symbol, timeframe);
            const candles = history.observations.map(({ candle, provenance }) => {
              if (provenance.sourceKind === "archive") throw new Error("TA requires collector observations");
              return { time: candle.openTime, open: candle.open, high: candle.high, low: candle.low,
                close: candle.close, volume: candle.baseVolume, complete: true,
                endpoint: provenance.endpoint,
                sourceEventTime: provenance.sourceKind === "websocket" ? provenance.sourceEventTime : null };
            });
            validate(candles);
            return {
              market: {
                source: history.identity.provider,
                instrument: { id: history.identity.instrumentId },
                priceType: history.identity.priceType, candles,
              },
              provider: null,
            };
          }
          const provider = await context.ta(symbol, timeframe, validate, requestedSource);
          return {
            market: {
              source: provider.source,
              instrument: provider.instrument,
              priceType: provider.priceType,
              candles: provider.candles.map((value) => ({
                ...value,
                endpoint: provider.endpoint,
                // REST klines carry no exchange event time; do not invent one.
                sourceEventTime: null,
              })),
            },
            provider,
          };
        };
        let generationMarket: MarketHistory | undefined;

        if (generationDue) {
          const loaded = await loadMarket();
          generationMarket = loaded.market;
          const candles = completedCandles(generationMarket.candles, timeframe, now);
          if (operationalStore?.enabled && !collectorStore && loaded.provider) {
            context.metrics.databaseWriteAttempts += 1;
            const nativeSymbol = sourceNativeSymbol(generationMarket.source, symbol);
            await operationalStore.recordCandles({
              userId,
              instrumentId: `${generationMarket.source}:${nativeSymbol}`,
              symbol,
              nativeSymbol,
              source: generationMarket.source,
              endpoint: loaded.provider.endpoint,
              priceType: generationMarket.priceType,
              timeframeMinutes: timeframe,
              retrievedAt: loaded.provider.retrievedAt,
              candles,
            });
          }
          const last = candles.at(-1)!;
          let candidates =
            latestAt === null
              ? [last]
              : candles.filter((candle) => candle.time > latestAt && candle.time <= expected);

          if (latestAt !== null && latestAt < candles[0]!.time) {
            throw new Error(
              `TA catch-up gap exceeds the available ${candles.length}-candle history`,
            );
          }
          if (candidates.length === 0) {
            throw new Error("Expected completed TA candle is not available");
          }
          candidates = candidates.slice(0, TA_CATCH_UP_LIMIT);
          const nativeSymbol = sourceNativeSymbol(generationMarket.source, symbol);
          const sourceInstrumentId = `${generationMarket.source}:${nativeSymbol}`;
          const requests: TechnicalAnalysisRequest[] = candidates.map((candle) => ({
            schema_version: 2,
            instrument: {
              instrument_id: sourceInstrumentId,
              exchange: generationMarket!.source,
              native_symbol: nativeSymbol,
              market_type: "futures",
              contract_type: "perpetual",
            },
            timeframe_minutes: timeframe,
            candles: requestCandles(candles),
            warmup_candles: [],
            missing_open_times_ms: [],
            source: generationMarket!.source,
            source_event_time_ms: candle.sourceEventTime,
            evaluation_time_ms: now,
            detection_time_ms: now,
            price_type: "trade",
            target_candle_open_time_ms: candle.time,
            config: {
              ta_version: TA_VERSION,
              interpretation_version: TA_INTERPRETATION_VERSION,
              minimum_history: TA_MINIMUM_HISTORY,
            },
          }));
          const results = await calculate(requests);
          const rows = results.map((result, index) => {
            const candle = candidates[index]!;
            const expectedCandleCount =
              candles.findIndex((value) => value.time === candle.time) + 1;
            if (
              result.status !== "ok" ||
              !result.indicators ||
              !result.factor_breakdown ||
              result.reason !== null ||
              result.timeframe_minutes !== timeframe ||
              result.candle_open_time_ms !== candle.time ||
              result.candle_close_time_ms !== candle.time + duration ||
              result.source_event_time_ms !== candle.sourceEventTime ||
              result.evaluation_time_ms !== now ||
              result.detection_time_ms !== now ||
              result.ta_version !== TA_VERSION ||
              result.strategy_version !== TA_INTERPRETATION_VERSION ||
              result.provenance.instrument_id !== sourceInstrumentId ||
              result.provenance.exchange !== generationMarket!.source ||
              result.provenance.source !== generationMarket!.source ||
              result.provenance.native_symbol !== nativeSymbol ||
              result.provenance.market_type !== "futures" ||
              result.provenance.contract_type !== "perpetual" ||
              result.provenance.price_type !== generationMarket!.priceType ||
              result.provenance.candle_count !== expectedCandleCount ||
              result.provenance.warmup_candle_count !== 0 ||
              result.provenance.missing_open_times_ms.length !== 0 ||
              result.indicators.candle_count !== expectedCandleCount ||
              result.indicators.candle.open_ms !== candle.time ||
              result.indicators.candle.open !== candle.open ||
              result.indicators.candle.high !== candle.high ||
              result.indicators.candle.low !== candle.low ||
              result.indicators.candle.close !== candle.close ||
              result.indicators.candle.volume !== candle.volume ||
              !result.indicators.candle.complete
            ) {
              throw new Error(
                `Python TA result unavailable or mismatched: ${result.reason ?? "invalid response"}`,
              );
            }
            context.metrics.taCalculations += 1;
            return {
              user_id: userId,
              symbol,
              instrument_id: generationMarket!.instrument.id,
              source_instrument_id: result.provenance.instrument_id,
              source_native_symbol: result.provenance.native_symbol,
              timeframe,
              source: generationMarket!.source,
              endpoint: candle.endpoint,
              price_type: generationMarket!.priceType,
              version: result.ta_version,
              strategy_version: result.strategy_version,
              candle_at: new Date(result.candle_open_time_ms).toISOString(),
              // Actual exchange event time is provenance; it is null when the source
              // has none. Candle completion is `candle_at + timeframe`.
              source_event_at:
                candle.sourceEventTime === null
                  ? null
                  : new Date(candle.sourceEventTime).toISOString(),
              evaluated_at: new Date(result.evaluation_time_ms).toISOString(),
              detected_at: new Date(result.detection_time_ms).toISOString(),
              price: result.indicators.candle.close,
              classification: result.classification,
              score: result.score,
              atr_pct: result.atr_pct,
              factor_breakdown: result.factor_breakdown as unknown as Json,
              reasons: result.reasons,
              indicators: result.indicators as unknown as Json,
              patterns: result.patterns,
            };
          });
          context.metrics.databaseWriteAttempts += 1;
          const { data: saved, error } = await db
            .from("ta_signals")
            .upsert(rows, {
              onConflict: "user_id,symbol,timeframe,candle_at,version",
              ignoreDuplicates: true,
            })
            .select("id");
          if (error) throw new Error(error.message);
          context.metrics.taSignalsSaved += saved?.length ?? 0;
        }

        if (pending.length === 0) return;
        const patches: OutcomePatch[] = [];
        for (const source of new Set(pending.map((row) => row.source))) {
          const relevant = pending.filter((row) => row.source === source);
          if (collectorStore && source !== MARKET_SOURCE) {
            // Collector mode holds no canonical history for another provider, so the
            // outcome is recorded as visibly unavailable rather than measured from a
            // silently substituted live series.
            for (const row of relevant) {
              patches.push({
                id: row.id,
                outcome_status: "unavailable",
                outcome_at: null,
                outcome_price: null,
                return_pct: null,
              });
            }
            continue;
          }
          const market =
            generationMarket?.source === source
              ? generationMarket
              : (await loadMarket(source)).market;
          const history = completedCandles(market.candles, timeframe, now);
          for (const row of relevant) {
            const target = outcomeDue(row.detected_at, timeframe);
            const candle = history.find((value) => value.time + duration === target);
            if (!candle) {
              if (target < history[0]!.time + duration) {
                patches.push({
                  id: row.id,
                  outcome_status: "unavailable",
                  outcome_at: null,
                  outcome_price: null,
                  return_pct: null,
                });
              }
              continue;
            }
            patches.push({
              id: row.id,
              outcome_status: "measured",
              outcome_at: new Date(candle.time + duration).toISOString(),
              outcome_price: candle.close,
              return_pct: (candle.close / row.price - 1) * 100,
            });
          }
        }
        if (patches.length > 0) {
          context.metrics.databaseWriteAttempts += 1;
          const { data: changed, error } = await db.rpc("apply_ta_outcomes", {
            p_user_id: userId,
            p_outcomes: patches,
          });
          if (error) throw new Error(error.message);
          context.metrics.taOutcomesUpdated += changed ?? 0;
          context.metrics.databaseNoOps += Math.max(0, patches.length - (changed ?? 0));
        }
      } catch (error) {
        errors.push(
          `TA ${symbol} ${timeframe}m: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }),
  );
  return errors;
}
