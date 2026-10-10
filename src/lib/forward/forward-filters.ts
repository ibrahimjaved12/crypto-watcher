import { z } from "zod";

/**
 * Strategy Lab filters (#239 P20), shared by the URL search params, the server functions and the
 * SQL calls. Dates are UTC `YYYY-MM-DD`; arrays are bounded; reward:risk comes from a fixed list
 * (the forward grid stores exact text, so 1.5 is "3/2").
 */
export const RR_VALUES = ["1", "3/2", "2", "3"] as const;
export const HORIZON_VALUES = [15, 60, 240] as const;
export const SIDE_VALUES = [1, -1] as const;
export const SYMBOL_VALUES = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT"] as const;
export const DAY_MS = 86_400_000;

const date = z.string().regex(/^\d{4}-\d{2}-\d{2}$/).refine((value) => !Number.isNaN(Date.parse(`${value}T00:00:00Z`)));
const family = z.string().regex(/^[a-z0-9_]{1,64}$/);

export const forwardFiltersSchema = z.object({
  from: date.optional(),
  to: date.optional(),
  families: z.array(family).max(16).default([]),
  horizons: z.array(z.union([z.literal(15), z.literal(60), z.literal(240)])).max(3).default([]),
  sides: z.array(z.union([z.literal(1), z.literal(-1)])).max(2).default([]),
  symbols: z.array(z.enum(SYMBOL_VALUES)).max(6).default([]),
  rr: z.array(z.enum(RR_VALUES)).max(4).default([]),
});
export type ForwardFilters = z.infer<typeof forwardFiltersSchema>;

/** Search-param validator that never throws: a bad value falls back to "no filter". */
export function parseSearch(search: Record<string, unknown>): ForwardFilters {
  const pick = <T,>(schema: z.ZodType<T, z.ZodTypeDef, unknown>, value: unknown, fallback: T) => {
    const parsed = schema.safeParse(value);
    return parsed.success ? parsed.data : fallback;
  };
  const shape = forwardFiltersSchema.shape;
  return {
    from: pick(shape.from, search["from"], undefined),
    to: pick(shape.to, search["to"], undefined),
    families: pick(shape.families, search["families"], []),
    horizons: pick(shape.horizons, search["horizons"], []),
    sides: pick(shape.sides, search["sides"], []),
    symbols: pick(shape.symbols, search["symbols"], []),
    rr: pick(shape.rr, search["rr"], []),
  };
}

export const utcDate = (ms: number) => new Date(ms).toISOString().slice(0, 10);

/** Default view: the last 30 UTC days up to today. */
export function withDefaultRange(filters: ForwardFilters, now = Date.now()): ForwardFilters {
  return { ...filters, from: filters.from ?? utcDate(now - 30 * DAY_MS), to: filters.to ?? utcDate(now) };
}

/** `to` is an inclusive date; the SQL range is [from 00:00, to + 1 day). */
export function rangeMs(filters: ForwardFilters): { fromMs: number | null; toMs: number | null } {
  return {
    fromMs: filters.from ? Date.parse(`${filters.from}T00:00:00Z`) : null,
    toMs: filters.to ? Date.parse(`${filters.to}T00:00:00Z`) + DAY_MS : null,
  };
}

/** Strategy ids are `<rule>:<minutes>`; a family filter expands over the three timeframes. */
export function strategyIdsFor(filters: ForwardFilters): string[] | null {
  if (!filters.families.length) return null;
  return filters.families.flatMap((name) => HORIZON_VALUES.map((minutes) => `${name}:${minutes}`));
}

/** The positional-by-name arguments shared by `forward_outcome_report` and `forward_nontrade_report`. */
export function reportRpcArgs(filters: ForwardFilters) {
  const { fromMs, toMs } = rangeMs(filters);
  return {
    p_from_ms: fromMs,
    p_to_ms: toMs,
    p_strategy_ids: strategyIdsFor(filters),
    p_symbols: filters.symbols.length ? filters.symbols : null,
    p_horizons: filters.horizons.length ? filters.horizons : null,
    p_sides: filters.sides.length ? filters.sides : null,
    p_rr: filters.rr.length ? filters.rr : null,
  };
}
