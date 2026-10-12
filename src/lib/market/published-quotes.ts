// Pure helpers around the hosted `market_quotes` table (#323): what the machine publishes, how the
// Worker maps a stored row back to the dashboard's `SymbolQuote`, and the freshness rules. No I/O and
// no server-only imports, so the publisher, the Worker read path, `mvp:check` and the tests share them.
import type { Check } from "../forward/mvp-check";
import type { QuoteFigures, QuoteCandle, SymbolQuote } from "./quote-calc";
import { QUOTE_STALE_MS, quoteStale } from "./quote-calc";
import { CHANGE_WINDOWS } from "./symbols";

/** Contract identity of every published quote (CLAUDE.md section 5): Binance USDⓈ-M trade price. */
export const PUBLISHED_SOURCE = "binance-usdm";
export const PUBLISHED_PRICE_TYPE = "trade";

/** One row as stored in `public.market_quotes`. */
export type MarketQuoteRow = {
  symbol: string;
  source: string;
  price_type: string;
  price: number;
  changes: unknown;
  chart: unknown;
  last_candle_at: string;
  published_at: string;
};

/** What the publisher sends (the database stamps `published_at`). */
export type PublishedQuoteInput = Omit<MarketQuoteRow, "published_at" | "changes" | "chart"> & {
  changes: Record<string, number | null>;
  chart: QuoteCandle[];
};

export function quoteInputFromFigures(symbol: string, figures: QuoteFigures): PublishedQuoteInput {
  const changes: Record<string, number | null> = {};
  for (const minutes of CHANGE_WINDOWS) changes[String(minutes)] = figures.changes[minutes] ?? null;
  return {
    symbol,
    source: PUBLISHED_SOURCE,
    price_type: PUBLISHED_PRICE_TYPE,
    price: figures.price,
    changes,
    chart: figures.chart,
    last_candle_at: new Date(figures.lastCandleAt).toISOString(),
  };
}

/**
 * Idempotency by symbol + last_candle_at: only quotes whose newest candle is newer than the one
 * already published (by this process) are sent. The database applies the same rule, so a restart
 * or a second publisher can never move a row backwards.
 */
export function quotesToPublish(
  candidates: readonly PublishedQuoteInput[],
  lastPublished: ReadonlyMap<string, number>,
): PublishedQuoteInput[] {
  return candidates.filter((quote) => {
    const previous = lastPublished.get(quote.symbol);
    return previous === undefined || Date.parse(quote.last_candle_at) > previous;
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseChanges(value: unknown): Record<number, number | null> | null {
  if (!isRecord(value)) return null;
  const changes: Record<number, number | null> = {};
  for (const minutes of CHANGE_WINDOWS) {
    const item = value[String(minutes)];
    if (item === undefined || item === null) changes[minutes] = null;
    else if (typeof item === "number" && Number.isFinite(item)) changes[minutes] = item;
    else return null;
  }
  return changes;
}

function parseChart(value: unknown): QuoteCandle[] | null {
  if (!Array.isArray(value)) return null;
  const chart: QuoteCandle[] = [];
  for (const point of value) {
    if (!isRecord(point)) return null;
    const { time, close } = point;
    if (typeof time !== "number" || !Number.isFinite(time)) return null;
    if (typeof close !== "number" || !Number.isFinite(close)) return null;
    chart.push({ time, close });
  }
  return chart;
}

function failedQuote(symbol: string, error: string, fetchedAt: string): SymbolQuote {
  return {
    symbol,
    ok: false,
    source: null,
    price: null,
    changes: {},
    chart: [],
    lastCandleAt: null,
    stale: true,
    error,
    fallbackReasons: [],
    fetchedAt,
  };
}

/** A watched symbol that has no published row: shown as such, never filled from another venue. */
export function missingQuote(symbol: string, now: number): SymbolQuote {
  return failedQuote(
    symbol,
    "Not collected yet — no price has been published for this pair.",
    new Date(now).toISOString(),
  );
}

/** Maps one stored row to the dashboard's `SymbolQuote`. Unreadable rows become visible failures. */
export function quoteFromRow(row: MarketQuoteRow, now: number): SymbolQuote {
  const fetchedAt = new Date(now).toISOString();
  if (row.source !== PUBLISHED_SOURCE || row.price_type !== PUBLISHED_PRICE_TYPE) {
    return failedQuote(
      row.symbol,
      `Published quote is not a Binance USDⓈ-M trade price (${row.source}/${row.price_type}).`,
      fetchedAt,
    );
  }
  const changes = parseChanges(row.changes);
  const chart = parseChart(row.chart);
  const lastCandleAt = Date.parse(row.last_candle_at);
  const publishedAt = Date.parse(row.published_at);
  const price = Number(row.price);
  if (
    !changes ||
    !chart ||
    !Number.isFinite(lastCandleAt) ||
    !Number.isFinite(publishedAt) ||
    !Number.isFinite(price) ||
    price <= 0
  ) {
    return failedQuote(row.symbol, "Published quote is unreadable.", fetchedAt);
  }
  return {
    symbol: row.symbol,
    ok: true,
    source: row.source,
    price,
    changes,
    chart,
    lastCandleAt,
    stale: quoteStale(lastCandleAt, now) || now - publishedAt > QUOTE_STALE_MS,
    error: null,
    fallbackReasons: [],
    fetchedAt,
  };
}

export type PublishedSnapshot = {
  quotes: SymbolQuote[];
  /** Newest `published_at` among the returned rows (ISO), or null when nothing is published. */
  publishedAt: string | null;
};

/** Quotes in the requested order; symbols without a row are "not collected yet". */
export function snapshotFromRows(
  symbols: readonly string[],
  rows: readonly MarketQuoteRow[],
  now: number,
): PublishedSnapshot {
  const bySymbol = new Map(rows.map((row) => [row.symbol, row] as const));
  let newest = Number.NEGATIVE_INFINITY;
  for (const symbol of symbols) {
    const published = Date.parse(bySymbol.get(symbol)?.published_at ?? "");
    if (Number.isFinite(published) && published > newest) newest = published;
  }
  return {
    quotes: symbols.map((symbol) => {
      const row = bySymbol.get(symbol);
      return row ? quoteFromRow(row, now) : missingQuote(symbol, now);
    }),
    publishedAt: Number.isFinite(newest) ? new Date(newest).toISOString() : null,
  };
}

/**
 * Banner state for the market page. `empty`: nothing published for the watched pairs; `stale`:
 * every readable quote is older than the threshold; `fresh` otherwise.
 */
export function publishedFreshness(
  snapshot: PublishedSnapshot,
): "empty" | "stale" | "fresh" {
  const readable = snapshot.quotes.filter((quote) => quote.ok);
  if (snapshot.publishedAt === null || readable.length === 0) return "empty";
  return readable.every((quote) => quote.stale) ? "stale" : "fresh";
}

function ageText(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  return seconds < 120 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
}

/** `mvp:check` lines: age of the newest `published_at` per symbol, WARN beyond the threshold. */
export function publishFreshnessChecks(
  expected: readonly string[],
  rows: readonly Pick<MarketQuoteRow, "symbol" | "published_at">[],
  now: number,
): Check[] {
  const bySymbol = new Map(rows.map((row) => [row.symbol, row] as const));
  return expected.map((symbol): Check => {
    const published = Date.parse(bySymbol.get(symbol)?.published_at ?? "");
    const name = `${symbol} published quote`;
    if (!Number.isFinite(published)) {
      return { name, level: "WARN", detail: "no row in market_quotes (the machine has not published it)" };
    }
    const age = now - published;
    return {
      name,
      level: age > QUOTE_STALE_MS ? "WARN" : "PASS",
      detail: `newest published_at ${ageText(age)} ago${age > QUOTE_STALE_MS ? " (older than 10 min; is the publisher running?)" : ""}`,
    };
  });
}
