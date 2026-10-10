import type { SupabaseClient } from "@supabase/supabase-js";
import { getOperationalStore } from "../operational/repository.server";
import { pythonServiceConfig } from "../python-service.server";
import { parseBinanceFunding, type FundingEvent } from "./forward-contract";
import { createForwardRepository } from "./forward-repository.server";
import type { ForwardDeps } from "./forward-run.server";
import {
  parseBinanceDailyKlines,
  parseBinanceFundingIntervals,
  type DailyBar,
} from "./forward-trend-contract";
import { createTrendRepository } from "./forward-trend-repository.server";
import {
  backfillMinuteHistory,
  KLINE_PAGE_LIMIT,
  type KlinePage,
} from "./forward-backfill.server";
import type { TrendDeps } from "./forward-trend-run.server";

/** Every forward strategy id of the Python registry (`forward.signals.FORWARD_STRATEGIES`). */
const TA_STRATEGIES = [
  "ema_cross_20_50",
  "ema_cross_50_200",
  "rsi_14_reversion",
  "macd_12_26_9",
  "bollinger_20_2_reversion",
  "keltner_breakout_20_14_2",
];
export const FORWARD_STRATEGY_IDS = TA_STRATEGIES.flatMap((name) =>
  [15, 60, 240].flatMap((minutes) => [`${name}:${minutes}`, `placebo-v1:${name}:${minutes}`]),
);

/** Production wiring: operational store reads, the Python service, the application DB (service role). */
export async function forwardDeps(send: typeof fetch = fetch): Promise<ForwardDeps> {
  const store = getOperationalStore();
  if (!store.enabled)
    throw new Error("The forward job needs the operational database (OPERATIONAL_DB_ENABLED=true)");
  // Relative import: the local job runs through Vite's module runner without the "@" alias.
  const { supabaseAdmin } = await import("../../integrations/supabase/client.server");
  return {
    now: () => Date.now(),
    readMinutes: (symbol, sinceMs, beforeMs) =>
      store.readForwardMinuteCandles(symbol, sinceMs, beforeMs),
    listHealth: (symbols) => store.listCollectorHealth(symbols),
    async callPython(body) {
      const config = pythonServiceConfig("/v1/forward/evaluate");
      const response = await send(config.url, {
        method: "POST",
        // P16: about 120 days of 1m rows per symbol (ewma-robust-hcal warm-up) make a large request.
        signal: AbortSignal.timeout(300_000),
        headers: { Authorization: `Bearer ${config.token}`, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(`Forward evaluation failed with HTTP ${response.status}`);
      return response.json();
    },
    fetchFunding: (symbol, startMs) => fetchBinanceFunding(send, symbol, startMs),
    // P16: one-time 1m history backfill through the conflict-safe collector write (P15).
    backfillMinutes: (symbol, startMs, endMs) =>
      backfillMinuteHistory(
        {
          now: () => Date.now(),
          fetchPage: (pair, from, to) => fetchBinanceMinuteKlines(send, pair, from, to),
          record: (candles) => store.recordCollectorCandles(candles),
          sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
          onProgress: (pair, page, total, candles) =>
            console.log(`[forward-backfill] ${pair} ${page}/${total} pages, ${candles} candles offered`),
        },
        symbol,
        startMs,
        endMs,
      ),
    repository: createForwardRepository(supabaseAdmin as unknown as SupabaseClient),
  };
}

/** One page of completed 1m klines (`/fapi/v1/klines?interval=1m`, limit 1500, weight 10). */
export async function fetchBinanceMinuteKlines(
  send: typeof fetch,
  symbol: string,
  startMs: number,
  endMs: number,
): Promise<KlinePage> {
  const url = new URL("https://fapi.binance.com/fapi/v1/klines");
  url.searchParams.set("symbol", symbol);
  url.searchParams.set("interval", "1m");
  url.searchParams.set("startTime", String(Math.max(0, Math.floor(startMs))));
  url.searchParams.set("endTime", String(Math.max(0, Math.floor(endMs))));
  url.searchParams.set("limit", String(KLINE_PAGE_LIMIT));
  const response = await send(url.toString(), { signal: AbortSignal.timeout(15_000) });
  if (response.status === 418 || response.status === 429) {
    throw new Error(`Binance 1m klines rate limited (HTTP ${response.status})`);
  }
  if (!response.ok) throw new Error(`Binance 1m klines failed with HTTP ${response.status}`);
  const weight = Number(response.headers.get("x-mbx-used-weight-1m"));
  return { rows: await response.json(), usedWeight: Number.isFinite(weight) && weight > 0 ? weight : null };
}

/** Public, unauthenticated Binance responses are rate limited by IP weight: 418/429 abort the call. */
async function binanceGet(send: typeof fetch, url: URL, what: string): Promise<unknown> {
  const response = await send(url.toString(), { signal: AbortSignal.timeout(10_000) });
  if (response.status === 418 || response.status === 429) {
    throw new Error(`Binance ${what} rate limited (HTTP ${response.status})`);
  }
  if (!response.ok) throw new Error(`Binance ${what} failed with HTTP ${response.status}`);
  return response.json();
}

/** Funding history from `startMs` (`/fapi/v1/fundingRate`, limit 1000): one request per symbol per run. */
export async function fetchBinanceFunding(
  send: typeof fetch,
  symbol: string,
  startMs: number,
): Promise<FundingEvent[]> {
  const url = new URL("https://fapi.binance.com/fapi/v1/fundingRate");
  url.searchParams.set("symbol", symbol);
  url.searchParams.set("startTime", String(Math.max(0, Math.floor(startMs))));
  url.searchParams.set("limit", "1000");
  return parseBinanceFunding(symbol, await binanceGet(send, url, "funding"));
}

/** Completed daily klines from `startMs` (`/fapi/v1/klines?interval=1d`, limit 1500, weight 10). */
export async function fetchBinanceDailyKlines(
  send: typeof fetch,
  symbol: string,
  startMs: number,
  nowMs: number,
): Promise<DailyBar[]> {
  const url = new URL("https://fapi.binance.com/fapi/v1/klines");
  url.searchParams.set("symbol", symbol);
  url.searchParams.set("interval", "1d");
  url.searchParams.set("startTime", String(Math.max(0, Math.floor(startMs))));
  url.searchParams.set("limit", "1500");
  return parseBinanceDailyKlines(symbol, await binanceGet(send, url, "klines"), nowMs);
}

/** Production wiring of the daily trend track (#239 P14). */
export async function forwardTrendDeps(send: typeof fetch = fetch): Promise<TrendDeps> {
  const store = getOperationalStore();
  if (!store.enabled)
    throw new Error(
      "The forward trend job needs the operational database (OPERATIONAL_DB_ENABLED=true)",
    );
  const { supabaseAdmin } = await import("../../integrations/supabase/client.server");
  return {
    now: () => Date.now(),
    readDailyBars: (symbol, sinceMs) => store.readForwardDailyBars(symbol, sinceMs),
    recordDailyBars: (bars, receivedAtMs) => store.recordForwardDailyBars(bars, receivedAtMs),
    fetchDailyKlines: (symbol, startMs, nowMs) =>
      fetchBinanceDailyKlines(send, symbol, startMs, nowMs),
    fetchFunding: (symbol, startMs) => fetchBinanceFunding(send, symbol, startMs),
    async fetchFundingIntervals() {
      const url = new URL("https://fapi.binance.com/fapi/v1/fundingInfo");
      return parseBinanceFundingIntervals(await binanceGet(send, url, "funding intervals"));
    },
    async callPython(body) {
      const config = pythonServiceConfig("/v1/forward/trend");
      const response = await send(config.url, {
        method: "POST",
        signal: AbortSignal.timeout(120_000),
        headers: { Authorization: `Bearer ${config.token}`, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok)
        throw new Error(`Forward trend evaluation failed with HTTP ${response.status}`);
      return response.json();
    },
    repository: createTrendRepository(supabaseAdmin as unknown as SupabaseClient),
  };
}
