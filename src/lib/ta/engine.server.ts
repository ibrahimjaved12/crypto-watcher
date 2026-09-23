import type { SupabaseClient } from "@supabase/supabase-js";
import { activityEnabled, logActivity } from "../activity-controls";
import type { Database, Json } from "@/integrations/supabase/types";
import { createMonitorRunContext, type MonitorRunContext } from "../monitor/run-context";
import { analyze, closedCandles, outcomeDue, TA_FRAMES, TA_VERSION } from "./core";

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

/** At most eight missed candles per frame are recovered in one run. */
export const TA_CATCH_UP_LIMIT = 8;

export function latestCompletedCandleAt(timeframe: number, now: number): number {
  const duration = timeframe * 60_000;
  return Math.floor(now / duration) * duration - duration;
}

/** Each frame fails independently; movement alerts do not depend on TA. */
export async function runTA(
  db: Client,
  userId: string,
  symbol: string,
  context: MonitorRunContext = createMonitorRunContext(),
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

        const validate = (candles: Parameters<typeof closedCandles>[0]) => {
          closedCandles(candles, timeframe, now);
        };
        let generationMarket: Awaited<ReturnType<MonitorRunContext["ta"]>> | undefined;

        if (generationDue) {
          generationMarket = await context.ta(symbol, timeframe, validate);
          const candles = closedCandles(generationMarket.candles, timeframe, now);
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
          const rows = candidates.map((candle) => {
            const index = candles.findIndex((value) => value.time === candle.time);
            const history = candles.slice(0, index + 1);
            if (history.length < 200) throw new Error("Insufficient history for TA catch-up");
            const indicators = analyze(history);
            context.metrics.taCalculations += 1;
            return {
              user_id: userId,
              symbol,
              instrument_id: generationMarket!.instrument.id,
              timeframe,
              source: generationMarket!.source,
              endpoint: generationMarket!.endpoint,
              price_type: generationMarket!.priceType,
              version: TA_VERSION,
              candle_at: new Date(candle.time).toISOString(),
              price: candle.close,
              indicators: indicators as unknown as Json,
              patterns: indicators.patterns,
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
          const market =
            generationMarket?.source === source
              ? generationMarket
              : await context.ta(symbol, timeframe, validate, source);
          const history = closedCandles(market.candles, timeframe, now);
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
