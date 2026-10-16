/**
 * Market-agnostic instrument identity and strategy/data compatibility (#374).
 * Mirrors python/market_analysis/instruments.py (same rules, same reason strings).
 * Shared by client and server; no node: imports.
 */
import { futuresInstrument, isSupportedSymbol } from "./symbols";

export const MARKETS = ["crypto", "forex"] as const;
export type Market = (typeof MARKETS)[number];

export const INSTRUMENT_KINDS = ["perpetual", "spot_cfd", "future"] as const;
export type InstrumentKind = (typeof INSTRUMENT_KINDS)[number];

export const ASSET_CLASSES = ["crypto", "fx", "metal", "energy", "index"] as const;
export type AssetClass = (typeof ASSET_CLASSES)[number];

export const DATA_FIELDS = [
  "ohlc",
  "volume_real",
  "volume_tick",
  "mark",
  "funding",
  "taker_flow",
  "open_interest",
  "spread",
] as const;
export type DataField = (typeof DATA_FIELDS)[number];

export type Instrument = {
  instrumentId: string;
  market: Market;
  kind: InstrumentKind;
  assetClass: AssetClass;
  base: string;
  quote: string;
  display: string;
};

/** How one venue names an instrument; contract details come from the broker, never guessed. */
export type VenueSymbol = {
  venue: string;
  nativeSymbol: string;
  instrumentId: string;
  digits: number | null;
  /** Decimal string, avoids float drift. */
  tickSize: string | null;
  /** Decimal string, avoids float drift. */
  contractSize: string | null;
  /** UTC epoch milliseconds, dated futures only. */
  expiryMs: number | null;
};

export type StrategyCapabilities = {
  markets: ReadonlySet<Market>;
  requires: ReadonlySet<DataField>;
  /** Each group needs at least one member present. */
  requiresAny?: readonly ReadonlySet<DataField>[];
  sessionAware?: boolean;
};

export type Compatibility = { ok: boolean; reasons: string[] };

export const CRYPTO_ID_PREFIX = "binance-usdm";
export const FOREX_SPOT_PREFIX = "fx";
export const FOREX_FUTURE_PREFIX = "fut";

const ID_PATTERN = /^[a-z0-9-]+:[A-Z0-9._-]+$/;
const DECIMAL_PATTERN = /^\d+(\.\d+)?$/;
const ALLOWED_KINDS: Record<Market, readonly InstrumentKind[]> = {
  crypto: ["perpetual"],
  forex: ["spot_cfd", "future"],
};
const ID_PREFIX_BY_KIND: Record<InstrumentKind, string> = {
  perpetual: CRYPTO_ID_PREFIX,
  spot_cfd: FOREX_SPOT_PREFIX,
  future: FOREX_FUTURE_PREFIX,
};

function requireText(name: string, value: unknown): void {
  if (typeof value !== "string" || value.trim() === "") throw new Error(`${name} must be a non-empty string`);
}

export function isMarket(value: string): value is Market {
  return (MARKETS as readonly string[]).includes(value);
}

/** For the dashboard toggle: anything unknown falls back. */
export function parseMarket(value: unknown, fallback: Market = "crypto"): Market {
  return typeof value === "string" && isMarket(value) ? value : fallback;
}

/** Validates and returns a frozen instrument. */
export function makeInstrument(fields: Instrument): Instrument {
  for (const name of ["instrumentId", "base", "quote", "display"] as const) requireText(name, fields[name]);
  if (!isMarket(fields.market)) throw new Error(`unknown market ${JSON.stringify(fields.market)}`);
  if (!(INSTRUMENT_KINDS as readonly string[]).includes(fields.kind))
    throw new Error(`unknown kind ${JSON.stringify(fields.kind)}`);
  if (!(ASSET_CLASSES as readonly string[]).includes(fields.assetClass))
    throw new Error(`unknown asset class ${JSON.stringify(fields.assetClass)}`);
  if (!ID_PATTERN.test(fields.instrumentId))
    throw new Error(
      `instrumentId ${JSON.stringify(fields.instrumentId)} must look like 'prefix:SYMBOL' (lowercase prefix, uppercase symbol)`,
    );
  const allowed = ALLOWED_KINDS[fields.market];
  if (!allowed.includes(fields.kind))
    throw new Error(
      `kind '${fields.kind}' is not valid for market '${fields.market}' (allowed: ${[...allowed].sort().join(", ")})`,
    );
  const prefix = ID_PREFIX_BY_KIND[fields.kind];
  if (fields.instrumentId.split(":", 1)[0] !== prefix)
    throw new Error(`instrumentId ${JSON.stringify(fields.instrumentId)} must start with '${prefix}:' for kind '${fields.kind}'`);
  return Object.freeze({ ...fields });
}

function positiveDecimal(name: string, value: string | null): void {
  if (value === null) return;
  if (!DECIMAL_PATTERN.test(value) || !/[1-9]/.test(value)) throw new Error(`${name} must be a positive decimal string`);
}

/** Validates and returns a frozen venue symbol; optional fields default to null. */
export function makeVenueSymbol(
  fields: Pick<VenueSymbol, "venue" | "nativeSymbol" | "instrumentId"> & Partial<VenueSymbol>,
): VenueSymbol {
  for (const name of ["venue", "nativeSymbol", "instrumentId"] as const) requireText(name, fields[name]);
  if (!ID_PATTERN.test(fields.instrumentId))
    throw new Error(`instrumentId ${JSON.stringify(fields.instrumentId)} must look like 'prefix:SYMBOL'`);
  const symbol: VenueSymbol = {
    venue: fields.venue,
    nativeSymbol: fields.nativeSymbol,
    instrumentId: fields.instrumentId,
    digits: fields.digits ?? null,
    tickSize: fields.tickSize ?? null,
    contractSize: fields.contractSize ?? null,
    expiryMs: fields.expiryMs ?? null,
  };
  if (symbol.digits !== null && (!Number.isInteger(symbol.digits) || symbol.digits < 0))
    throw new Error("digits must be an integer >= 0");
  positiveDecimal("tickSize", symbol.tickSize);
  positiveDecimal("contractSize", symbol.contractSize);
  if (symbol.expiryMs !== null && (!Number.isSafeInteger(symbol.expiryMs) || symbol.expiryMs <= 0))
    throw new Error("expiryMs must be a positive UTC epoch millisecond integer");
  return Object.freeze(symbol);
}

/** Binance USDⓈ-M perpetual; restricted to SUPPORTED_SYMBOLS, id stays `binance-usdm:SYMBOL`. */
export function cryptoInstrument(symbol: string): Instrument {
  if (!isSupportedSymbol(symbol)) throw new Error(`Unsupported futures contract: ${symbol}`);
  const futures = futuresInstrument(symbol);
  return makeInstrument({
    instrumentId: futures.id,
    market: "crypto",
    kind: "perpetual",
    assetClass: "crypto",
    base: futures.baseAsset,
    quote: futures.quoteAsset,
    display: futures.nativeSymbol,
  });
}

/** Forex instrument; base/quote default to 3+3 letters only for a six-letter pair symbol. */
export function forexInstrument(
  symbol: string,
  options: { kind?: InstrumentKind; assetClass?: AssetClass; base?: string; quote?: string; display?: string } = {},
): Instrument {
  requireText("symbol", symbol);
  const native = symbol.trim().toUpperCase();
  const kind = options.kind ?? "spot_cfd";
  if (!ALLOWED_KINDS.forex.includes(kind)) throw new Error(`kind '${kind}' is not valid for market 'forex'`);
  let { base, quote } = options;
  if (/^[A-Z]{6}$/.test(native)) {
    base ||= native.slice(0, 3);
    quote ||= native.slice(3);
  }
  if (!base || !quote)
    throw new Error(`forex symbol ${JSON.stringify(symbol)} is not a six-letter pair: base and quote are required`);
  return makeInstrument({
    instrumentId: `${ID_PREFIX_BY_KIND[kind]}:${native}`,
    market: "forex",
    kind,
    assetClass: options.assetClass ?? "fx",
    base: base.toUpperCase(),
    quote: quote.toUpperCase(),
    display: options.display || native,
  });
}

export const FOREX_DEFAULTS: readonly Instrument[] = Object.freeze([
  forexInstrument("XAUUSD", { assetClass: "metal", base: "XAU", quote: "USD", display: "Gold" }),
  forexInstrument("EURUSD"),
  forexInstrument("USDJPY"),
  forexInstrument("USOIL", { assetClass: "energy", base: "USOIL", quote: "USD", display: "WTI crude" }),
]);

export const FOREX_OPTIONAL: readonly Instrument[] = Object.freeze([
  forexInstrument("DXY", { assetClass: "index", base: "DXY", quote: "USD", display: "US Dollar Index" }),
  forexInstrument("UKOIL", { assetClass: "energy", base: "UKOIL", quote: "USD", display: "Brent crude" }),
]);

export const CRYPTO_FIELDS: ReadonlySet<DataField> = new Set<DataField>([
  "ohlc",
  "volume_real",
  "mark",
  "funding",
  "taker_flow",
  "open_interest",
]);
export const FOREX_FIELDS: ReadonlySet<DataField> = new Set<DataField>(["ohlc", "volume_tick", "spread"]);

/** Fields the instrument's market provides; a futures venue also has real traded volume. */
export function providedFields(instrument: Instrument): ReadonlySet<DataField> {
  if (instrument.market === "crypto") return CRYPTO_FIELDS;
  if (instrument.kind === "future") return new Set<DataField>([...FOREX_FIELDS, "volume_real"]);
  return FOREX_FIELDS;
}

/** Fail closed. Reason order: no capabilities, market, missing fields, any-of groups. */
export function checkCompatibility(
  caps: StrategyCapabilities | null | undefined,
  instrument: Instrument,
  provided?: ReadonlySet<DataField>,
): Compatibility {
  if (!caps) return { ok: false, reasons: ["no_capabilities_declared"] };
  if (caps.markets.size === 0) throw new Error("markets must not be empty");
  const reasons: string[] = [];
  if (!caps.markets.has(instrument.market)) reasons.push(`market_not_supported:${instrument.market}`);
  const have = provided ?? providedFields(instrument);
  const missing = [...caps.requires].filter((field) => !have.has(field)).sort();
  for (const field of missing) reasons.push(`missing_field:${field}`);
  for (const group of caps.requiresAny ?? []) {
    if (group.size === 0) throw new Error("requiresAny groups must not be empty");
    if (![...group].some((field) => have.has(field))) reasons.push(`missing_any_of:${[...group].sort().join("|")}`);
  }
  return { ok: reasons.length === 0, reasons };
}
