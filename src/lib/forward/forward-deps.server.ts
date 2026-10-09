import type { SupabaseClient } from "@supabase/supabase-js";
import { getOperationalStore } from "../operational/repository.server";
import { pythonServiceConfig } from "../python-service.server";
import { parseBinanceFunding } from "./forward-contract";
import { createForwardRepository } from "./forward-repository.server";
import type { ForwardDeps } from "./forward-run.server";

/** Every forward strategy id of the Python registry (`forward.signals.FORWARD_STRATEGIES`). */
const TA_STRATEGIES = ["ema_cross_20_50", "ema_cross_50_200", "rsi_14_reversion", "macd_12_26_9",
  "bollinger_20_2_reversion", "keltner_breakout_20_14_2"];
export const FORWARD_STRATEGY_IDS = TA_STRATEGIES.flatMap((name) =>
  [15, 60, 240].flatMap((minutes) => [`${name}:${minutes}`, `placebo-v1:${name}:${minutes}`]));

/** Production wiring: operational store reads, the Python service, the application DB (service role). */
export async function forwardDeps(send: typeof fetch = fetch): Promise<ForwardDeps> {
  const store = getOperationalStore();
  if (!store.enabled) throw new Error("The forward job needs the operational database (OPERATIONAL_DB_ENABLED=true)");
  // Relative import: the local job runs through Vite's module runner without the "@" alias.
  const { supabaseAdmin } = await import("../../integrations/supabase/client.server");
  return {
    now: () => Date.now(),
    readMinutes: (symbol, sinceMs, beforeMs) => store.readForwardMinuteCandles(symbol, sinceMs, beforeMs),
    listHealth: (symbols) => store.listCollectorHealth(symbols),
    async callPython(body) {
      const config = pythonServiceConfig("/v1/forward/evaluate");
      const response = await send(config.url, {
        method: "POST",
        signal: AbortSignal.timeout(120_000),
        headers: { Authorization: `Bearer ${config.token}`, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(`Forward evaluation failed with HTTP ${response.status}`);
      return response.json();
    },
    async fetchFunding(symbol, startMs) {
      // Public, unauthenticated funding history (weight-limited by IP): one request per symbol per run.
      const url = new URL("https://fapi.binance.com/fapi/v1/fundingRate");
      url.searchParams.set("symbol", symbol);
      url.searchParams.set("startTime", String(Math.max(0, Math.floor(startMs))));
      url.searchParams.set("limit", "1000");
      const response = await send(url.toString(), { signal: AbortSignal.timeout(10_000) });
      if (response.status === 418 || response.status === 429) {
        throw new Error(`Binance funding rate limited (HTTP ${response.status})`);
      }
      if (!response.ok) throw new Error(`Binance funding failed with HTTP ${response.status}`);
      return parseBinanceFunding(symbol, await response.json());
    },
    repository: createForwardRepository(supabaseAdmin as unknown as SupabaseClient),
  };
}
