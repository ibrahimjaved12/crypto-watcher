import type { SupabaseClient } from "@supabase/supabase-js";
import type { Database } from "@/integrations/supabase/types";
import { loadTACandles } from "../market/providers.server";
import { analyze, closedCandles, outcomeDue, TA_FRAMES, TA_VERSION } from "./core";

type Client = Pick<SupabaseClient<Database>, "from">;

/** Each frame fails independently; movement alerts do not depend on TA. */
export async function runTA(db: Client, userId: string, symbol: string) {
  const errors: string[] = [];
  await Promise.all(
    TA_FRAMES.map(async (timeframe) => {
      try {
        const now = Date.now();
        const validate = (candles: Parameters<typeof closedCandles>[0]) => {
          closedCandles(candles, timeframe, now);
        };
        const market = await loadTACandles(symbol, timeframe, validate);
        const candles = closedCandles(market.candles, timeframe, now);
        const last = candles.at(-1)!;
        const indicators = analyze(candles);
        const { error } = await db.from("ta_signals").upsert(
          {
            user_id: userId,
            symbol,
            timeframe,
            source: market.source,
            version: TA_VERSION,
            candle_at: new Date(last.time).toISOString(),
            price: last.close,
            indicators,
            patterns: indicators.patterns,
          },
          { onConflict: "user_id,symbol,timeframe,candle_at,version", ignoreDuplicates: true },
        );
        if (error) throw new Error(error.message);

        const { data: pending, error: readError } = await db
          .from("ta_signals")
          .select("id,source,detected_at,price")
          .eq("user_id", userId)
          .eq("symbol", symbol)
          .eq("timeframe", timeframe)
          .eq("outcome_status", "pending")
          .order("detected_at")
          .limit(500);
        if (readError) throw new Error(readError.message);
        // Never evaluate a fallback exchange against the original exchange's price.
        for (const source of new Set((pending ?? []).map((row) => row.source))) {
          const relevant = (pending ?? []).filter((row) => row.source === source);
          const duration = timeframe * 60_000;
          const target = (detectedAt: string) => outcomeDue(detectedAt, timeframe);
          if (!relevant.some((row) => target(row.detected_at) <= now)) continue;
          const history =
            source === market.source
              ? candles
              : closedCandles(
                  (await loadTACandles(symbol, timeframe, validate, source)).candles,
                  timeframe,
                  now,
                );
          for (const row of relevant) {
            const candle = history.find((c) => c.time + duration === target(row.detected_at));
            if (!candle) {
              if (target(row.detected_at) < history[0]!.time + duration) {
                const { error: expiredError } = await db
                  .from("ta_signals")
                  .update({ outcome_status: "unavailable" })
                  .eq("id", row.id)
                  .eq("user_id", userId)
                  .eq("outcome_status", "pending");
                if (expiredError) throw new Error(expiredError.message);
              }
              continue;
            }
            const { error: updateError } = await db
              .from("ta_signals")
              .update({
                outcome_at: new Date(candle.time + duration).toISOString(),
                outcome_price: candle.close,
                return_pct: (candle.close / row.price - 1) * 100,
                outcome_status: "measured",
              })
              .eq("id", row.id)
              .eq("user_id", userId)
              .eq("outcome_status", "pending");
            if (updateError) throw new Error(updateError.message);
          }
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
