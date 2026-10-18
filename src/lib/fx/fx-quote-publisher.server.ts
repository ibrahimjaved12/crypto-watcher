// Machine-side forex quote publisher (#433, part of #374), the forex twin of the crypto
// quote-publisher (#323). Every minute (after a short settle) it reads the subscribed instruments,
// their completed 1m BID bars and the broker's symbol specs (#432) from the local operational store,
// builds each quote with fx-quote-calc.ts and sends ONE batched upsert to the hosted `fx_market_quotes`
// (readable only by users entitled to forex). It makes no broker call. Every step here is caught, and
// the hosted function is forward-only, so a restart or a retry can never move a row backwards.
import type { Json } from "../../integrations/supabase/types";
import type { OperationalStore } from "../operational/types";
import { fxQuoteFromBars, type FxQuoteInput } from "./fx-quote-calc";

const MINUTE_MS = 60_000;
/** Wait after a minute boundary so the forex collector has stored the bar that just closed. */
const SETTLE_MS = 3_000;
/** First run after start-up. */
const START_DELAY_MS = 5_000;
/** The newest bars per symbol: more than the 1440-minute window plus its 60-minute gap tolerance. */
const BAR_LIMIT = 2_000;
const LOG = "[fx-quote]";

export type FxQuotePublisherDeps = {
  store: Pick<OperationalStore, "getFxSubscriptions" | "getFxBars" | "getFxSymbolSpecs">;
  /** Sends one batch to the hosted table; resolves with the number of rows written. */
  publish(quotes: FxQuoteInput[]): Promise<number>;
  log?: (line: string) => void;
};

export type FxPublishState = {
  /** Newest published last_bar_at (ms) per symbol, for this process. */
  lastPublished: Map<string, number>;
  /** Symbols already reported as having no spec (logged once per process). */
  warnedNoSpec: Set<string>;
};

export type FxPublishRun = {
  symbols: number;
  published: number;
  /** Subscribed symbols without a stored symbol spec. */
  noSpec: string[];
  /** Symbols with no bars yet or a failed local read. */
  unavailable: string[];
  writeError: string | null;
};

export function newFxPublishState(): FxPublishState {
  return { lastPublished: new Map(), warnedNoSpec: new Set() };
}

/**
 * One publishing pass: build quotes for every subscribed symbol that has a spec, send those whose
 * newest bar advanced in ONE batch, and remember them only after the hosted write succeeded (a failed
 * write is retried on the next pass). Never throws; failures are reported in the result.
 */
export async function publishFxQuotesOnce(deps: FxQuotePublisherDeps, state: FxPublishState): Promise<FxPublishRun> {
  const log = deps.log ?? ((line: string) => console.info(line));
  const run: FxPublishRun = { symbols: 0, published: 0, noSpec: [], unavailable: [], writeError: null };

  let subscriptions: Awaited<ReturnType<FxQuotePublisherDeps["store"]["getFxSubscriptions"]>>;
  let specs: Awaited<ReturnType<FxQuotePublisherDeps["store"]["getFxSymbolSpecs"]>>;
  try {
    [subscriptions, specs] = await Promise.all([deps.store.getFxSubscriptions(), deps.store.getFxSymbolSpecs()]);
  } catch (error) {
    run.unavailable.push(`store: ${error instanceof Error ? error.message : String(error)}`);
    return run;
  }
  run.symbols = subscriptions.length;
  const specByKey = new Map(specs.map((spec) => [`${spec.venue}|${spec.instrumentId}`, spec]));

  const candidates: FxQuoteInput[] = [];
  for (const subscription of subscriptions) {
    const symbol = subscription.instrumentId.split(":")[1] ?? subscription.instrumentId;
    const spec = specByKey.get(`${subscription.venue}|${subscription.instrumentId}`);
    if (!spec) {
      run.noSpec.push(symbol);
      if (!state.warnedNoSpec.has(symbol)) {
        state.warnedNoSpec.add(symbol);
        log(`${LOG} ${symbol}: no symbol spec yet`);
      }
      continue;
    }
    try {
      // The newest bars whatever their age, so a closure only shows as null windows (fx-quote-calc).
      const bars = await deps.store.getFxBars(subscription.venue, subscription.instrumentId, { limit: BAR_LIMIT });
      const quote = fxQuoteFromBars(bars, spec);
      if (quote) candidates.push(quote);
      else run.unavailable.push(symbol);
    } catch {
      run.unavailable.push(symbol);
    }
  }

  const batch = candidates.filter((quote) => {
    const previous = state.lastPublished.get(quote.symbol);
    return previous === undefined || Date.parse(quote.last_bar_at) > previous;
  });
  if (batch.length === 0) return run;
  try {
    run.published = await deps.publish(batch);
    for (const quote of batch) state.lastPublished.set(quote.symbol, Date.parse(quote.last_bar_at));
  } catch (error) {
    run.writeError = error instanceof Error ? error.message : String(error);
  }
  return run;
}

type HostedFxPublishClient = {
  rpc(name: "publish_fx_market_quotes", args: { p_quotes: Json }): PromiseLike<{
    data: unknown;
    error: { message: string } | null;
  }>;
};

/** Capture the startup-loaded client; publishing must not import through a closed Vite runner. */
export function createHostedFxPublish(client: HostedFxPublishClient): FxQuotePublisherDeps["publish"] {
  return async (quotes) => {
    const { data, error } = await client.rpc("publish_fx_market_quotes", {
      p_quotes: quotes as unknown as Json,
    });
    if (error) throw new Error(error.message);
    return typeof data === "number" ? data : 0;
  };
}

export type FxQuotePublisher = { stop(): void };

/**
 * Starts the periodic publisher, only from the standalone entrypoint. Returns null (and says why)
 * when the hosted service credentials or the store are missing; it never throws.
 */
export function startFxQuotePublisher(options: {
  env?: Record<string, string | undefined>;
  deps?: FxQuotePublisherDeps;
  /** Clock for the minute schedule (the pass itself is clock-free). */
  now?: () => number;
  log?: (line: string) => void;
  error?: (line: string) => void;
} = {}): FxQuotePublisher | null {
  const clock = options.now ?? Date.now;
  const env = options.env ?? process.env;
  const log = options.log ?? ((line: string) => console.info(line));
  const error = options.error ?? ((line: string) => console.error(line));
  try {
    if (!env["SUPABASE_URL"] || !env["SUPABASE_SERVICE_ROLE_KEY"]) {
      log(`${LOG} not started: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required to publish to hosted Supabase`);
      return null;
    }
    const deps = options.deps;
    if (!deps?.store) {
      log(`${LOG} not started: no operational store was provided`);
      return null;
    }
    const passDeps = { ...deps, log: deps.log ?? log };
    const state = newFxPublishState();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let writeFailuresInRow = 0;

    const schedule = (delay: number): void => {
      if (stopped) return;
      timer = setTimeout(() => void tick(), delay);
    };
    const tick = async (): Promise<void> => {
      try {
        const run = await publishFxQuotesOnce(passDeps, state);
        if (run.writeError) {
          writeFailuresInRow += 1;
          error(`${LOG} hosted write failed (${writeFailuresInRow} in a row), retrying next minute: ${run.writeError.slice(0, 200)}`);
        } else {
          writeFailuresInRow = 0;
        }
      } catch (caught) {
        // publishFxQuotesOnce does not throw; last line of defence.
        error(`${LOG} unexpected failure: ${caught instanceof Error ? caught.message : String(caught)}`);
      }
      const now = clock();
      schedule(Math.floor(now / MINUTE_MS) * MINUTE_MS + MINUTE_MS + SETTLE_MS - now);
    };

    schedule(START_DELAY_MS);
    log(`${LOG} started; publishes after each completed 1m bar, no broker calls`);
    return {
      stop() {
        stopped = true;
        if (timer) clearTimeout(timer);
      },
    };
  } catch (caught) {
    error(`${LOG} not started: ${caught instanceof Error ? caught.message : String(caught)}`);
    return null;
  }
}
