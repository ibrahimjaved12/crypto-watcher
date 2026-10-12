// Machine-side market quote publisher (#323). It reads the completed candles the collector already
// stored in the local operational store, builds each quote with the shared functions in
// `quote-calc.ts` and sends ONE batched upsert of all collected symbols to the hosted
// `market_quotes` table after each completed 1m candle. It makes no exchange call. It runs as a
// sibling child of the worker host (src/worker/market-quote-publisher.ts), never inside the collector,
// so the collector artifact holds no service-role credential. Every step here is caught.
import type { Json } from "../../integrations/supabase/types";
import type { OperationalStore } from "../operational/types";
import {
  quoteInputFromFigures,
  quotesToPublish,
  type PublishedQuoteInput,
} from "./published-quotes";
import { QUOTE_MINUTE_BARS, QUOTE_QUARTER_BARS, quoteFigures, type QuoteCandle } from "./quote-calc";

const MINUTE_MS = 60_000;
/** Wait after a minute boundary so the collector has stored the candle that just closed. */
const SETTLE_MS = 3_000;
/** First run after start-up: gives the collector time to subscribe and bootstrap. */
const START_DELAY_MS = 5_000;
/** Quick retries inside one minute while some symbols' newest candle has not been stored yet. */
const BEHIND_RETRY_MS = 5_000;
const MAX_BEHIND_RETRIES = 4;

export type QuotePublisherDeps = {
  store: Pick<OperationalStore, "readCollectorSubscriptions" | "readCollectorCompletedCandles">;
  /** Sends one batch to the hosted table; resolves with the number of rows written. */
  publish(quotes: PublishedQuoteInput[]): Promise<number>;
  now?: () => number;
};

export type PublishState = {
  /** Newest published last_candle_at (ms) per symbol, for this process. */
  lastPublished: Map<string, number>;
};

export type PublishRun = {
  symbols: number;
  published: number;
  /** Symbols whose newest stored 1m candle is older than the last completed minute. */
  behind: string[];
  /** Symbols with no usable candles (not bootstrapped yet) or a failed local read. */
  unavailable: string[];
  writeError: string | null;
};

export function newPublishState(): PublishState {
  return { lastPublished: new Map() };
}

function candles(series: {
  observations: ReadonlyArray<{ candle: { openTime: number; close: number } }>;
}): QuoteCandle[] {
  return series.observations.map(({ candle }) => ({ time: candle.openTime, close: candle.close }));
}

/**
 * One publishing pass: build quotes for every collected symbol, send those whose newest candle
 * advanced, remember them only after the hosted write succeeded (a failed write is therefore retried
 * on the next pass). Never throws; local read and hosted write failures are reported in the result.
 */
export async function publishQuotesOnce(
  deps: QuotePublisherDeps,
  state: PublishState,
): Promise<PublishRun> {
  const now = (deps.now ?? Date.now)();
  const lastCompletedOpen = Math.floor(now / MINUTE_MS) * MINUTE_MS - MINUTE_MS;
  const run: PublishRun = { symbols: 0, published: 0, behind: [], unavailable: [], writeError: null };

  let symbols: string[];
  try {
    symbols = [...new Set((await deps.store.readCollectorSubscriptions()).map((s) => s.toUpperCase()))];
  } catch (error) {
    run.unavailable.push(`subscriptions: ${error instanceof Error ? error.message : String(error)}`);
    return run;
  }
  run.symbols = symbols.length;

  const candidates: PublishedQuoteInput[] = [];
  for (const symbol of symbols) {
    try {
      const [minute, quarter] = await Promise.all([
        deps.store.readCollectorCompletedCandles(symbol, 1, QUOTE_MINUTE_BARS),
        deps.store.readCollectorCompletedCandles(symbol, 15, QUOTE_QUARTER_BARS),
      ]);
      const figures = quoteFigures(candles(minute), candles(quarter));
      if (!figures) {
        run.unavailable.push(symbol);
        continue;
      }
      if (figures.lastCandleAt < lastCompletedOpen) run.behind.push(symbol);
      candidates.push(quoteInputFromFigures(symbol, figures));
    } catch {
      run.unavailable.push(symbol);
    }
  }

  const batch = quotesToPublish(candidates, state.lastPublished);
  if (batch.length === 0) return run;
  try {
    run.published = await deps.publish(batch);
    for (const quote of batch) {
      state.lastPublished.set(quote.symbol, Date.parse(quote.last_candle_at));
    }
  } catch (error) {
    run.writeError = error instanceof Error ? error.message : String(error);
  }
  return run;
}

export type MarketQuotePublisher = { stop(): void };

export type StartPublisherOptions = {
  env?: Record<string, string | undefined>;
  /** The collector's operational store (the caller already has it). */
  store?: QuotePublisherDeps["store"];
  /** Full override of the store and hosted write. Used by tests. */
  deps?: QuotePublisherDeps;
  log?: (line: string) => void;
  error?: (line: string) => void;
};

/** Default hosted write: the same service-role client the machine's forward jobs use. */
function hostedPublish(): QuotePublisherDeps["publish"] {
  return async (quotes) => {
    const { supabaseAdmin } = await import("../../integrations/supabase/client.server");
    const { data, error } = await supabaseAdmin.rpc("publish_market_quotes", {
      p_quotes: quotes as unknown as Json,
    });
    if (error) throw new Error(error.message);
    return typeof data === "number" ? data : 0;
  };
}

/**
 * Starts the periodic publisher, only from the standalone entrypoint. Its pending timer must keep
 * the process alive until stop() is called. Returns null (and says why) when it is disabled or the
 * hosted service credentials are not configured; it never throws.
 * Set MARKET_QUOTE_PUBLISHER_ENABLED=false to turn it off.
 */
export function startMarketQuotePublisher(
  options: StartPublisherOptions = {},
): MarketQuotePublisher | null {
  const env = options.env ?? process.env;
  const log = options.log ?? ((line: string) => console.info(line));
  const error = options.error ?? ((line: string) => console.error(line));
  try {
    if (env["MARKET_QUOTE_PUBLISHER_ENABLED"] === "false") {
      log("[market-quote-publisher] disabled by MARKET_QUOTE_PUBLISHER_ENABLED=false");
      return null;
    }
    let deps = options.deps;
    if (!deps) {
      if (!env["SUPABASE_URL"] || !env["SUPABASE_SERVICE_ROLE_KEY"]) {
        log(
          "[market-quote-publisher] not started: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required to publish to hosted Supabase",
        );
        return null;
      }
      if (!options.store) {
        log("[market-quote-publisher] not started: no operational store was provided");
        return null;
      }
      deps = { store: options.store, publish: hostedPublish() };
    }

    const state = newPublishState();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let behindRetries = 0;
    let writeFailuresInRow = 0;
    let newestPublishedAt: string | null = null;

    const schedule = (delay: number): void => {
      if (stopped) return;
      timer = setTimeout(() => void tick(), delay);
    };

    const tick = async (): Promise<void> => {
      let retrySoon = false;
      try {
        const run = await publishQuotesOnce(deps!, state);
        if (run.writeError) {
          writeFailuresInRow += 1;
          error(
            `[market-quote-publisher] hosted write failed (${writeFailuresInRow} in a row), retrying on the next candle: ${run.writeError.slice(0, 200)}`,
          );
        } else {
          writeFailuresInRow = 0;
          if (run.published > 0) newestPublishedAt = new Date().toISOString();
        }
        log(
          `[market-quote-publisher] heartbeat symbols=${run.symbols} published=${run.published} behind=${run.behind.length} unavailable=${run.unavailable.length} newest_published_at=${newestPublishedAt ?? "never"}`,
        );
        retrySoon = !run.writeError && run.behind.length > 0 && behindRetries < MAX_BEHIND_RETRIES;
      } catch (caught) {
        // publishQuotesOnce does not throw; this is the last line of defence for the collector.
        error(
          `[market-quote-publisher] unexpected failure: ${caught instanceof Error ? caught.message : String(caught)}`,
        );
      }
      if (retrySoon) {
        behindRetries += 1;
        schedule(BEHIND_RETRY_MS);
      } else {
        behindRetries = 0;
        const now = Date.now();
        schedule(Math.floor(now / MINUTE_MS) * MINUTE_MS + MINUTE_MS + SETTLE_MS - now);
      }
    };

    schedule(START_DELAY_MS);
    log("[market-quote-publisher] started; publishes after each completed 1m candle, no exchange calls");
    return {
      stop() {
        stopped = true;
        if (timer) clearTimeout(timer);
      },
    };
  } catch (caught) {
    error(
      `[market-quote-publisher] not started: ${caught instanceof Error ? caught.message : String(caught)}`,
    );
    return null;
  }
}
