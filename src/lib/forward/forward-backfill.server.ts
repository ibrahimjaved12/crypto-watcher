import { z } from "zod";
import type { CollectorCandle } from "../operational/types";

/**
 * One-minute history backfill for the forward engine (#239 P16). The ewma-robust-hcal sigma needs
 * 60 days of completed horizon windows after the 28-day slot-factor warm-up, about 120 days of 1m
 * candles, while the live collector only holds what it has seen. On a run whose stored history
 * starts too late, the missing older range is fetched from the public, unauthenticated
 * `GET /fapi/v1/klines?interval=1m` (limit 1500, request weight 10), oldest page first, and written
 * through `record_collector_candles`: idempotent, stored candles are never overwritten, and a
 * differing candle becomes a conflict row (P15 policy), never a failed batch.
 *
 * Rate limits: Binance reports the IP's used weight in `X-MBX-USED-WEIGHT-1M` (limit 2400/min).
 * Above `WEIGHT_PAUSE_AT` the backfill waits for the next minute; HTTP 418/429 aborts the backfill
 * (the next run resumes, because the range is derived from what is stored).
 */
export const KLINE_PAGE_LIMIT = 1500;
export const WEIGHT_PAUSE_AT = 1500;
const MINUTE_MS = 60_000;
const ENDPOINT = "/fapi/v1/klines";

const decimal = z.string().regex(/^\d+(\.\d+)?$/);
/** [openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, takerBase, takerQuote, ignore] */
export const binanceMinuteKlineSchema = z
  .tuple([
    z.number().int().nonnegative(),
    decimal,
    decimal,
    decimal,
    decimal,
    decimal,
    z.number().int().nonnegative(),
    decimal,
    z.number().int().nonnegative(),
    decimal,
    decimal,
    z.unknown(),
  ])
  .refine(([openTime, , , , , , closeTime]) => openTime % MINUTE_MS === 0 && closeTime === openTime + MINUTE_MS - 1);
export const binanceMinuteKlinesSchema = z.array(binanceMinuteKlineSchema).max(KLINE_PAGE_LIMIT);

export type KlinePage = { rows: unknown; usedWeight: number | null };

export type BackfillDeps = {
  now(): number;
  /** One page from `startMs` (inclusive) to `endMs` (inclusive), limit 1500. */
  fetchPage(symbol: string, startMs: number, endMs: number): Promise<KlinePage>;
  record(candles: CollectorCandle[]): Promise<unknown>;
  sleep(ms: number): Promise<void>;
  /** Called after each page: 1-based page index, estimated pages in this range, candles offered so far. */
  onProgress?(symbol: string, pageIndex: number, pagesTotalEstimate: number, candlesOffered: number): void;
};

/** Completed candles of one validated page (a candle still open at `nowMs` is dropped). */
export function parseMinuteKlines(symbol: string, value: unknown, nowMs: number, receivedAt: number): CollectorCandle[] {
  const parsed = binanceMinuteKlinesSchema.safeParse(value);
  if (!parsed.success) throw new Error("Invalid Binance 1m kline response");
  const native = symbol.toUpperCase();
  let previous = -1;
  const out: CollectorCandle[] = [];
  for (const [openTime, open, high, low, close, volume, closeTime, quoteVolume] of parsed.data) {
    if (openTime <= previous) throw new Error("Invalid Binance 1m kline response: order");
    previous = openTime;
    if (closeTime >= nowMs) continue;
    const [o, h, l, c] = [open, high, low, close].map(Number) as [number, number, number, number];
    if (!(o > 0 && h > 0 && l > 0 && c > 0) || h < Math.max(o, c) || l > Math.min(o, c))
      throw new Error("Invalid Binance 1m kline response: prices");
    out.push({
      instrumentId: `binance-usdm:${native}`,
      symbol: native,
      nativeSymbol: native,
      provider: "binance-usdm",
      endpoint: ENDPOINT,
      priceType: "trade",
      timeframeMinutes: 1,
      openTime,
      closeTime,
      open: o,
      high: h,
      low: l,
      close: c,
      volume: Number(volume),
      quoteVolume: Number(quoteVolume),
      sourceEventTime: null,
      receivedAt,
      transport: "rest",
    });
  }
  return out;
}

/**
 * Fetch and record [startMs, endMs) for one symbol, oldest first. Returns the candles offered.
 * An empty page (before listing, or minutes Binance never published) is skipped, not an end: the
 * loop continues to `endMs`. Never requests the still-open minute. A thrown error (418/429, bad
 * page) aborts after the pages already recorded; the caller derives the remaining holes from what
 * is stored.
 */
export async function backfillMinuteHistory(
  deps: BackfillDeps,
  symbol: string,
  startMs: number,
  endMs: number,
): Promise<number> {
  let cursor = Math.ceil(startMs / MINUTE_MS) * MINUTE_MS;
  const last = Math.min(endMs, Math.floor(deps.now() / MINUTE_MS) * MINUTE_MS) - MINUTE_MS;
  let offered = 0;
  let pageIndex = 0;
  const pagesTotal = Math.max(1, Math.ceil((last - cursor + MINUTE_MS) / (KLINE_PAGE_LIMIT * MINUTE_MS)));
  while (cursor <= last) {
    const pageEnd = Math.min(last, cursor + (KLINE_PAGE_LIMIT - 1) * MINUTE_MS);
    const page = await deps.fetchPage(symbol, cursor, pageEnd + MINUTE_MS - 1);
    const now = deps.now();
    const candles = parseMinuteKlines(symbol, page.rows, now, now).filter(
      (candle) => candle.openTime >= cursor && candle.openTime <= pageEnd,
    );
    if (candles.length) {
      for (let index = 0; index < candles.length; index += 1000) {
        await deps.record(candles.slice(index, index + 1000));
      }
      offered += candles.length;
    }
    cursor = pageEnd + MINUTE_MS;
    pageIndex += 1;
    deps.onProgress?.(symbol, pageIndex, pagesTotal, offered);
    if (page.usedWeight !== null && page.usedWeight >= WEIGHT_PAUSE_AT) {
      await deps.sleep(MINUTE_MS - (deps.now() % MINUTE_MS) + 1_000);
    }
  }
  return offered;
}
