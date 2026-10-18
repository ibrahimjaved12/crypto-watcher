// Pure helpers for the hosted `fx_market_quotes` (#433, #398): the row the Worker reads, the quote the
// forex Market page shows, its freshness state and number formatting. No I/O and no server-only imports,
// so the server function, the page and the tests share them.

/** UNVERIFIED placeholder: a quote is live while its newest 1m bar opened at most 3 minutes ago. The bar
 * that opened one minute ago is the newest completed one, and the publisher runs a few seconds after
 * each minute, so a healthy feed sits at 1 to 2 minutes. */
export const FX_LIVE_MAX_AGE_MS = 3 * 60_000;
/** UNVERIFIED placeholder: the change window the tile shows (minutes). */
export const FX_TILE_CHANGE_WINDOW = "60";

const CHANGE_KEYS = ["5", "15", "60", "240", "1440"] as const;

/** One row of `public.fx_market_quotes`. */
export type FxQuoteRow = {
  symbol: string;
  instrument_id: string;
  venue: string;
  bid: number;
  ask: number;
  spread_points: number;
  digits: number;
  changes: unknown;
  chart: unknown;
  last_bar_at: string;
  published_at: string;
};

export type FxQuote = {
  symbol: string;
  instrumentId: string;
  venue: string;
  bid: number;
  ask: number;
  spreadPoints: number;
  digits: number;
  /** Minutes -> percent change of the bid; null when the window could not be computed. */
  changes: Record<string, number | null>;
  /** 15-minute bid closes, oldest first. */
  chart: { time: number; close: number }[];
  /** Open time (ms) of the newest completed 1m bar. */
  lastBarAt: number;
  publishedAt: number;
  /** now - lastBarAt when read (never negative). */
  ageMs: number;
};

export type FxQuoteState = "live" | "stale" | "closed" | "none";
export type FxFeedState = "live" | "delayed" | "none" | "closed";

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

/** A stored row as the page's quote, or null when the row is malformed (it is then shown as no data). */
export function toFxQuote(row: FxQuoteRow, now: number): FxQuote | null {
  const lastBarAt = Date.parse(row.last_bar_at);
  const publishedAt = Date.parse(row.published_at);
  if (!finite(row.bid) || row.bid <= 0 || !finite(row.ask) || row.ask < row.bid || !Number.isInteger(row.digits)
      || !Number.isInteger(row.spread_points) || !finite(lastBarAt)) return null;
  const rawChanges = row.changes && typeof row.changes === "object" && !Array.isArray(row.changes)
    ? (row.changes as Record<string, unknown>) : {};
  const changes: Record<string, number | null> = {};
  for (const key of CHANGE_KEYS) changes[key] = finite(rawChanges[key]) ? rawChanges[key] : null;
  const chart = Array.isArray(row.chart)
    ? row.chart.flatMap((point: unknown) => {
        const p = point as { time?: unknown; close?: unknown } | null;
        return p && finite(p.time) && finite(p.close) ? [{ time: p.time, close: p.close }] : [];
      })
    : [];
  return {
    symbol: row.symbol,
    instrumentId: row.instrument_id,
    venue: row.venue,
    bid: row.bid,
    ask: row.ask,
    spreadPoints: row.spread_points,
    digits: row.digits,
    changes,
    chart,
    lastBarAt,
    publishedAt: finite(publishedAt) ? publishedAt : lastBarAt,
    ageMs: Math.max(0, now - lastBarAt),
  };
}

/** none: no row; closed: the market is closed (the price is the last one); live: open and fresh; else stale. */
export function fxQuoteState(quote: FxQuote | null | undefined, now: number, marketOpen: boolean): FxQuoteState {
  if (!quote) return "none";
  if (!marketOpen) return "closed";
  return now - quote.lastBarAt <= FX_LIVE_MAX_AGE_MS ? "live" : "stale";
}

/** The page's feed chip: no rows at all, the market closed, any live tile, or quotes but none live. */
export function fxFeedState(states: readonly FxQuoteState[], marketOpen: boolean): FxFeedState {
  if (!states.some((state) => state !== "none")) return "none";
  if (!marketOpen) return "closed";
  return states.includes("live") ? "live" : "delayed";
}

/** Fixed digits, never an exponent ("–" when not a number). */
export function formatPrice(value: number, digits: number): string {
  if (!finite(value)) return "–";
  const places = Math.min(Math.max(Math.trunc(digits), 0), 10);
  return value.toFixed(places);
}

/** The price split so the last digit group can be dimmed: "1.085" + "12", "2412." + "37". */
export function splitPrice(text: string, digits: number): [string, string] {
  const tail = Math.min(2, Math.max(0, digits));
  return tail === 0 || text.length <= tail ? [text, ""] : [text.slice(0, -tail), text.slice(-tail)];
}

/**
 * Spread for display. 3- and 5-digit symbols quote a fractional pip, so 10 points are one pip. For any
 * other precision (2-digit metals, oil, indices) a "pip" is not one agreed size (gold's is often 0.1 =
 * 10 points, sometimes 0.01), so the spread is shown in points rather than guessed.
 */
export function spreadPips(spreadPoints: number, digits: number): { value: number; unit: "pips" | "pts" } {
  return digits === 3 || digits === 5
    ? { value: spreadPoints / 10, unit: "pips" }
    : { value: spreadPoints, unit: "pts" };
}

export function formatSpread(spreadPoints: number, digits: number): string {
  const { value, unit } = spreadPips(spreadPoints, digits);
  return `${unit === "pips" ? value.toFixed(1) : String(value)} ${unit}`;
}

export function changeTone(pct: number | null | undefined): "bull" | "bear" | "muted" {
  if (!finite(pct) || pct === 0) return "muted";
  return pct > 0 ? "bull" : "bear";
}

/** "0.12%" with two decimals, unsigned (the arrow carries the sign); null for no value. */
export function formatChange(pct: number | null | undefined): string | null {
  return finite(pct) ? `${Math.abs(pct).toFixed(2)}%` : null;
}
