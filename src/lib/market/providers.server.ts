/** Public perpetual-futures data. Server-only; no API key is required. */
import {
  futuresInstrument,
  isMarketSource,
  isSupportedSymbol,
  MARKET_PRICE_TYPE,
  MARKET_SOURCE,
  MARKET_SOURCES,
  type FuturesInstrument,
  type MarketSource,
} from "./symbols";

export type Candle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  /** Exact Binance quote-asset volume when supplied by USD-M klines. */
  quoteVolume?: number;
  complete?: boolean;
};

export type FuturesContract = FuturesInstrument & {
  status: "TRADING";
  listedAt: number;
  expiresAt: number | null;
  priceTick: string | null;
  quantityStep: string | null;
  minQuantity: string | null;
  minNotional: string | null;
};

export type ProviderResult = {
  source: MarketSource;
  instrument: FuturesContract;
  endpoint: string;
  priceType: typeof MARKET_PRICE_TYPE;
  retrievedAt: string;
  minute: Candle[];
  quarter: Candle[];
};

export type TACandleResult = {
  source: MarketSource;
  instrument: FuturesContract;
  endpoint: string;
  priceType: typeof MARKET_PRICE_TYPE;
  retrievedAt: string;
  candles: Candle[];
};

/**
 * A completed candle read back from the leased collector's operational store. Its
 * provenance is recorded at ingestion (#26), so unlike the REST provider result it
 * carries the exact endpoint/transport that produced this candle, the exchange
 * kline close time, the actual source event time, and the collector receive time.
 */
export type CollectorTACandle = Candle & {
  closeTime: number;
  /**
   * Actual exchange event time (WebSocket `E`), or null for REST bootstrap/recovery,
   * which has no exchange event. It is provenance only; candle completion is defined
   * by `time + timeframeMinutes`.
   */
  sourceEventTime: number | null;
  receivedAt: number;
  endpoint: string;
  transport: "rest" | "websocket";
};

/**
 * Canonical completed candles for the application-owned TA path (#20). Provenance is
 * per candle because one series can mix WebSocket live candles with REST
 * bootstrap/recovery candles; the application must not reconstruct or fabricate it.
 */
export type CollectorTACandleHistory = {
  source: MarketSource;
  instrument: FuturesContract;
  priceType: typeof MARKET_PRICE_TYPE;
  candles: CollectorTACandle[];
};

export type ProviderObserver = {
  request(source: MarketSource, kind: "metadata" | "candles", timeframe?: number): void;
  candleRows(source: MarketSource, timeframe: number, rows: number): void;
};

export type FuturesSnapshot = {
  source: typeof MARKET_SOURCE;
  instrument: FuturesContract;
  lastPrice: number;
  lastPriceAt: number;
  markPrice: number;
  indexPrice: number;
  fundingRate: number | null;
  fundingAt: number | null;
  retrievedAt: string;
};

const BASE_URLS: Record<MarketSource, string> = {
  "binance-usdm": "https://fapi.binance.com",
  "okx-usdt-swap": "https://www.okx.com",
  "kraken-futures": "https://futures.kraken.com",
};
const ENDPOINTS: Record<MarketSource, string> = {
  "binance-usdm": "/fapi/v1/klines",
  "okx-usdt-swap": "/api/v5/market/candles",
  "kraken-futures": "/api/charts/v1/trade/:symbol/:resolution",
};
const MINUTE_LIMIT = 61;
const QUARTER_LIMIT = 97;
const METADATA_TTL_MS = 5 * 60_000;

type BinanceSymbol = {
  symbol?: string;
  pair?: string;
  contractType?: string;
  status?: string;
  baseAsset?: string;
  quoteAsset?: string;
  marginAsset?: string;
  onboardDate?: number;
  deliveryDate?: number;
  filters?: Array<Record<string, unknown>>;
};

type OkxInstrument = {
  instId?: string;
  instType?: string;
  ctType?: string;
  settleCcy?: string;
  state?: string;
  listTime?: string;
  tickSz?: string;
  lotSz?: string;
  minSz?: string;
};

let metadataCache = new Map<string, { expiresAt: number; body: unknown }>();

async function fetchJson(url: string, timeoutMs = 8000): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: { accept: "application/json" },
    });
    if (!res.ok) {
      try {
        await res.body?.cancel();
      } catch {
        // Cleanup must not replace the HTTP error.
      }
      throw new Error(`HTTP ${res.status}`);
    }
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

async function cachedJson(url: string, onMiss?: () => void): Promise<unknown> {
  const now = Date.now();
  const cached = metadataCache.get(url);
  if (cached && cached.expiresAt > now) return cached.body;
  onMiss?.();
  const body = await fetchJson(url);
  metadataCache.set(url, { expiresAt: now + METADATA_TTL_MS, body });
  return body;
}

/** Clears process-wide provider metadata for deterministic tests. */
export function clearExchangeInfoCache(): void {
  metadataCache = new Map();
}

function positiveDecimal(value: unknown, label: string): string {
  if (typeof value !== "string" || !/^\d+(\.\d+)?$/.test(value) || Number(value) <= 0) {
    throw new Error(`invalid ${label}`);
  }
  return value;
}

function finiteNumber(value: unknown, label: string): number {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) throw new Error(`invalid ${label}`);
  return number;
}

function finiteNonnegative(value: unknown, label: string): number {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) throw new Error(`invalid ${label}`);
  return number;
}

function providerSymbol(source: MarketSource, symbol: string): string {
  const base = symbol.replace(/USDT$/, "");
  if (source === "okx-usdt-swap") return `${base}-USDT-SWAP`;
  if (source === "kraken-futures") return `PF_${base === "BTC" ? "XBT" : base}USD`;
  return symbol;
}

function fallbackContract(symbol: string): FuturesContract {
  return {
    ...futuresInstrument(symbol),
    status: "TRADING",
    listedAt: 0,
    expiresAt: null,
    priceTick: null,
    quantityStep: null,
    minQuantity: null,
    minNotional: null,
  };
}

async function resolveInstrument(
  source: MarketSource,
  symbol: string,
  observer?: ProviderObserver,
): Promise<FuturesContract> {
  const normalized = symbol.toUpperCase();
  if (!isSupportedSymbol(normalized))
    throw new Error(`unsupported futures contract: ${normalized}`);
  const canonical = fallbackContract(normalized);

  if (source === "kraken-futures") return canonical;
  if (source === "okx-usdt-swap") {
    const native = providerSymbol(source, normalized);
    const url = `${BASE_URLS[source]}/api/v5/public/instruments?instType=SWAP&instId=${encodeURIComponent(native)}`;
    const body = (await cachedJson(url, () => observer?.request(source, "metadata"))) as {
      code?: string;
      data?: OkxInstrument[];
    };
    const item = body.code === "0" && Array.isArray(body.data) ? body.data[0] : undefined;
    if (
      !item ||
      item.instId !== native ||
      item.instType !== "SWAP" ||
      item.ctType !== "linear" ||
      item.settleCcy !== "USDT" ||
      item.state !== "live"
    ) {
      throw new Error(`inactive or incompatible futures contract: ${normalized}`);
    }
    const listedAt = Number(item.listTime);
    if (!Number.isSafeInteger(listedAt))
      throw new Error(`invalid futures contract dates: ${normalized}`);
    return {
      ...canonical,
      listedAt,
      priceTick: positiveDecimal(item.tickSz, "price tick"),
      quantityStep: positiveDecimal(item.lotSz, "quantity step"),
      minQuantity: positiveDecimal(item.minSz, "minimum quantity"),
    };
  }

  const body = (await cachedJson(`${BASE_URLS[source]}/fapi/v1/exchangeInfo`, () =>
    observer?.request(source, "metadata"),
  )) as {
    symbols?: BinanceSymbol[];
  };
  if (!Array.isArray(body.symbols)) throw new Error("invalid exchange information");
  const item = body.symbols.find((entry) => entry.symbol === normalized);
  if (
    !item ||
    item.status !== "TRADING" ||
    item.contractType !== "PERPETUAL" ||
    item.pair !== normalized ||
    item.baseAsset !== canonical.baseAsset ||
    item.quoteAsset !== "USDT" ||
    item.marginAsset !== "USDT"
  ) {
    throw new Error(`inactive or incompatible futures contract: ${normalized}`);
  }
  const filter = (type: string) => item.filters?.find((value) => value["filterType"] === type);
  const priceFilter = filter("PRICE_FILTER");
  const lotFilter = filter("LOT_SIZE");
  const notionalFilter = filter("MIN_NOTIONAL");
  if (!Number.isSafeInteger(item.onboardDate) || !Number.isSafeInteger(item.deliveryDate)) {
    throw new Error(`invalid futures contract dates: ${normalized}`);
  }
  return {
    ...canonical,
    listedAt: item.onboardDate!,
    expiresAt: item.deliveryDate! >= 4_102_444_800_000 ? null : item.deliveryDate!,
    priceTick: positiveDecimal(priceFilter?.["tickSize"], "price tick"),
    quantityStep: positiveDecimal(lotFilter?.["stepSize"], "quantity step"),
    minQuantity: positiveDecimal(lotFilter?.["minQty"], "minimum quantity"),
    minNotional: positiveDecimal(notionalFilter?.["notional"], "minimum notional"),
  };
}

function parseBinance(raw: unknown, minutes: number, now = Date.now()): Candle[] {
  if (!Array.isArray(raw) || raw.length === 0) throw new Error("empty kline response");
  const duration = minutes * 60_000;
  return raw.map((row) => {
    if (!Array.isArray(row) || row.length < 8) throw new Error("invalid kline row");
    const time = Number(row[0]);
    const closeTime = Number(row[6]);
    if (!Number.isSafeInteger(time) || closeTime !== time + duration - 1) {
      throw new Error("invalid candle timestamp");
    }
    return {
      time,
      open: finiteNumber(row[1], "open price"),
      high: finiteNumber(row[2], "high price"),
      low: finiteNumber(row[3], "low price"),
      close: finiteNumber(row[4], "close price"),
      volume: finiteNonnegative(row[5], "volume"),
      quoteVolume: finiteNonnegative(row[7], "quote volume"),
      complete: closeTime < now,
    };
  });
}

export type BinanceKlineRange = {
  limit?: number;
  startTime?: number;
  endTime?: number;
  now?: number;
};

/** Exact Binance USD-M trade-price klines for collector bootstrap and bounded recovery. */
export async function loadBinanceFuturesKlines(
  symbol: string,
  minutes: 1 | 15 | 60 | 240,
  range: BinanceKlineRange = {},
): Promise<TACandleResult> {
  const instrument = await resolveInstrument(MARKET_SOURCE, symbol);
  const limit = range.limit ?? 250;
  if (!Number.isInteger(limit) || limit < 1 || limit > 1000) throw new Error("invalid kline limit");
  const interval = minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`;
  const query = new URLSearchParams({
    symbol: instrument.nativeSymbol,
    interval,
    limit: String(limit),
  });
  for (const [name, value] of [
    ["startTime", range.startTime],
    ["endTime", range.endTime],
  ] as const) {
    if (value !== undefined) {
      if (!Number.isSafeInteger(value) || value < 0) throw new Error(`invalid ${name}`);
      query.set(name, String(value));
    }
  }
  const raw = await fetchJson(`${BASE_URLS[MARKET_SOURCE]}${ENDPOINTS[MARKET_SOURCE]}?${query}`);
  const retrievedAtMs = range.now ?? Date.now();
  return {
    source: MARKET_SOURCE,
    instrument,
    endpoint: ENDPOINTS[MARKET_SOURCE],
    priceType: MARKET_PRICE_TYPE,
    retrievedAt: new Date(retrievedAtMs).toISOString(),
    candles: parseBinance(raw, minutes, retrievedAtMs),
  };
}

function parseOkx(raw: unknown, minutes: number): Candle[] {
  const body = raw as { code?: string; data?: unknown[] };
  if (body?.code !== "0" || !Array.isArray(body.data) || body.data.length === 0) {
    throw new Error("empty kline response");
  }
  const duration = minutes * 60_000;
  return body.data
    .map((row) => {
      if (!Array.isArray(row) || row.length < 9) throw new Error("invalid kline row");
      const time = Number(row[0]);
      if (!Number.isSafeInteger(time) || time % duration !== 0 || !["0", "1"].includes(row[8])) {
        throw new Error("invalid candle timestamp");
      }
      return {
        time,
        open: finiteNumber(row[1], "open price"),
        high: finiteNumber(row[2], "high price"),
        low: finiteNumber(row[3], "low price"),
        close: finiteNumber(row[4], "close price"),
        volume: finiteNonnegative(row[6], "volume"),
        complete: row[8] === "1",
      };
    })
    .sort((a, b) => a.time - b.time);
}

function parseKraken(raw: unknown, minutes: number): Candle[] {
  const rows = (raw as { candles?: unknown[] })?.candles;
  if (!Array.isArray(rows) || rows.length === 0) throw new Error("empty kline response");
  const duration = minutes * 60_000;
  return rows
    .map((value) => {
      const row = value as Record<string, unknown>;
      const time = Number(row["time"]);
      if (!Number.isSafeInteger(time) || time % duration !== 0) {
        throw new Error("invalid candle timestamp");
      }
      return {
        time,
        open: finiteNumber(row["open"], "open price"),
        high: finiteNumber(row["high"], "high price"),
        low: finiteNumber(row["low"], "low price"),
        close: finiteNumber(row["close"], "close price"),
        volume: finiteNonnegative(row["volume"], "volume"),
        complete: time + duration <= Date.now(),
      };
    })
    .sort((a, b) => a.time - b.time);
}

async function klines(
  source: MarketSource,
  symbol: string,
  minutes: number,
  limit: number,
  observer?: ProviderObserver,
): Promise<Candle[]> {
  const native = providerSymbol(source, symbol);
  observer?.request(source, "candles", minutes);
  let candles: Candle[];
  if (source === "okx-usdt-swap") {
    const bar = minutes >= 60 ? `${minutes / 60}H` : `${minutes}m`;
    const query = new URLSearchParams({ instId: native, bar, limit: String(limit) });
    candles = parseOkx(
      await fetchJson(`${BASE_URLS[source]}${ENDPOINTS[source]}?${query}`),
      minutes,
    );
  } else if (source === "kraken-futures") {
    const resolution = minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`;
    const path = `/api/charts/v1/trade/${encodeURIComponent(native)}/${resolution}`;
    candles = parseKraken(await fetchJson(`${BASE_URLS[source]}${path}?count=${limit}`), minutes);
  } else {
    const interval = minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`;
    const query = new URLSearchParams({ symbol: native, interval, limit: String(limit) });
    candles = parseBinance(
      await fetchJson(`${BASE_URLS[source]}${ENDPOINTS[source]}?${query}`),
      minutes,
    );
  }
  observer?.candleRows(source, minutes, candles.length);
  return candles;
}

async function load(
  source: MarketSource,
  symbol: string,
  intervals: [number, number],
  limits: [number, number],
  observer?: ProviderObserver,
): Promise<ProviderResult> {
  const instrument = await resolveInstrument(source, symbol, observer);
  const first = klines(source, symbol, intervals[0], limits[0], observer);
  const [minute, quarter] = await Promise.all([
    first,
    intervals[0] === intervals[1]
      ? first
      : klines(source, symbol, intervals[1], limits[1], observer),
  ]);
  return {
    source,
    instrument,
    endpoint: ENDPOINTS[source],
    priceType: MARKET_PRICE_TYPE,
    retrievedAt: new Date().toISOString(),
    minute,
    quarter,
  };
}

export async function loadTACandles(
  symbol: string,
  minutes: number,
  validate: (candles: Candle[]) => void,
  requestedSource?: string,
  observer?: ProviderObserver,
): Promise<TACandleResult> {
  let sources: readonly MarketSource[] = MARKET_SOURCES;
  if (requestedSource) {
    if (!isMarketSource(requestedSource))
      throw new Error(`Unknown data source: ${requestedSource}`);
    sources = [requestedSource];
  }
  const errors: string[] = [];
  for (const source of sources) {
    try {
      const result = await load(source, symbol, [minutes, minutes], [250, 250], observer);
      validate(result.minute);
      return {
        source: result.source,
        instrument: result.instrument,
        endpoint: result.endpoint,
        priceType: result.priceType,
        retrievedAt: result.retrievedAt,
        candles: result.minute,
      };
    } catch (error) {
      errors.push(`${source}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  throw new Error(errors.join(" | "));
}

export type ProviderOutcome =
  { ok: true; result: ProviderResult } | { ok: false; errors: string[] };

export async function loadCandles(
  symbol: string,
  validate?: (result: ProviderResult) => void,
  observer?: ProviderObserver,
): Promise<ProviderOutcome> {
  const errors: string[] = [];
  for (const source of MARKET_SOURCES) {
    try {
      const result = await load(source, symbol, [1, 15], [MINUTE_LIMIT, QUARTER_LIMIT], observer);
      if (result.minute.length < 2 || result.quarter.length < 2)
        throw new Error("insufficient data");
      validate?.(result);
      return { ok: true, result };
    } catch (error) {
      errors.push(`${source}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  return { ok: false, errors };
}

/** Monitoring needs only the completed 1m observation, not the dashboard's 15m chart. */
export async function loadObservationCandles(
  symbol: string,
  validate?: (result: ProviderResult) => void,
  observer?: ProviderObserver,
): Promise<ProviderOutcome> {
  const errors: string[] = [];
  for (const source of MARKET_SOURCES) {
    try {
      const result = await load(source, symbol, [1, 1], [MINUTE_LIMIT, MINUTE_LIMIT], observer);
      if (result.minute.length < 2) throw new Error("insufficient data");
      validate?.(result);
      return { ok: true, result };
    } catch (error) {
      errors.push(`${source}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  return { ok: false, errors };
}

/** Binance-only trade, mark, index and funding snapshot helper. */
export async function loadFuturesSnapshot(symbol: string): Promise<FuturesSnapshot> {
  const instrument = await resolveInstrument(MARKET_SOURCE, symbol);
  const encoded = encodeURIComponent(instrument.nativeSymbol);
  const [tickerRaw, premiumRaw, fundingRaw] = await Promise.all([
    fetchJson(`${BASE_URLS[MARKET_SOURCE]}/fapi/v2/ticker/price?symbol=${encoded}`),
    fetchJson(`${BASE_URLS[MARKET_SOURCE]}/fapi/v1/premiumIndex?symbol=${encoded}`),
    fetchJson(`${BASE_URLS[MARKET_SOURCE]}/fapi/v1/fundingRate?symbol=${encoded}&limit=1`),
  ]);
  const ticker = tickerRaw as { symbol?: string; price?: string; time?: number };
  const premium = premiumRaw as { symbol?: string; markPrice?: string; indexPrice?: string };
  const funding = Array.isArray(fundingRaw)
    ? (fundingRaw[0] as { fundingRate?: string; fundingTime?: number } | undefined)
    : undefined;
  if (ticker.symbol !== instrument.nativeSymbol || premium.symbol !== instrument.nativeSymbol) {
    throw new Error("mismatched futures snapshot identity");
  }
  if (!Number.isSafeInteger(Number(ticker.time))) throw new Error("invalid last price time");
  return {
    source: MARKET_SOURCE,
    instrument,
    lastPrice: finiteNumber(ticker.price, "last price"),
    lastPriceAt: Number(ticker.time),
    markPrice: finiteNumber(premium.markPrice, "mark price"),
    indexPrice: finiteNumber(premium.indexPrice, "index price"),
    fundingRate:
      funding && Number.isFinite(Number(funding.fundingRate)) ? Number(funding.fundingRate) : null,
    fundingAt:
      funding && Number.isSafeInteger(Number(funding.fundingTime))
        ? Number(funding.fundingTime)
        : null,
    retrievedAt: new Date().toISOString(),
  };
}
