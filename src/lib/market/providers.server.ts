/**
 * Public exchange data providers. Server-only.
 *
 * No API keys are required for these public market endpoints. Providers are
 * tried in order; if every provider fails (for example because the hosting
 * region is blocked) we report the failure instead of inventing prices.
 */

export type Candle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  complete?: boolean;
};

export type ProviderResult = {
  source: string;
  minute: Candle[]; // 1m candles, oldest -> newest
  quarter: Candle[]; // 15m candles, oldest -> newest
};

const MINUTE_LIMIT = 61; // covers 5m / 15m / 1h
const QUARTER_LIMIT = 97; // covers 4h / 24h and the chart

async function fetchJson(url: string, timeoutMs = 8000): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: { accept: "application/json" },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

async function binance(
  symbol: string,
  intervals: [number, number] = [1, 15],
  limits: [number, number] = [MINUTE_LIMIT, QUARTER_LIMIT],
): Promise<ProviderResult> {
  const get = async (interval: string, limit: number): Promise<Candle[]> => {
    const raw = (await fetchJson(
      `https://api.binance.com/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`,
    )) as unknown[];
    if (!Array.isArray(raw) || raw.length === 0) throw new Error("empty response");
    return raw.map((row) => {
      const r = row as [number, string, string, string, string, string, number];
      const duration =
        (interval.endsWith("h")
          ? Number(interval.slice(0, -1)) * 60
          : Number(interval.slice(0, -1))) * 60_000;
      if (Number(r[6]) !== Number(r[0]) + duration - 1) {
        throw new Error("Invalid candle close timestamp");
      }
      return {
        time: Number(r[0]),
        open: Number(r[1]),
        high: Number(r[2]),
        low: Number(r[3]),
        close: Number(r[4]),
        volume: Number(r[5]),
        complete: Number(r[6]) < Date.now(),
      };
    });
  };
  const format = (m: number) => (m >= 60 ? `${m / 60}h` : `${m}m`);
  const first = get(format(intervals[0]), limits[0]);
  const [minute, quarter] = await Promise.all([
    first,
    intervals[0] === intervals[1] ? first : get(format(intervals[1]), limits[1]),
  ]);
  return { source: "Binance", minute, quarter };
}

async function okx(
  symbol: string,
  intervals: [number, number] = [1, 15],
  limits: [number, number] = [MINUTE_LIMIT, QUARTER_LIMIT],
): Promise<ProviderResult> {
  const instId = `${symbol.replace(/USDT$/, "")}-USDT`;
  const get = async (bar: string, limit: number): Promise<Candle[]> => {
    const body = (await fetchJson(
      `https://www.okx.com/api/v5/market/candles?instId=${instId}&bar=${bar}&limit=${limit}`,
    )) as { code?: string; msg?: string; data?: string[][] };
    if (body.code !== "0" || !body.data?.length) {
      throw new Error(body.msg || "empty response");
    }
    // OKX returns newest first.
    return body.data
      .map((r) => ({
        time: Number(r[0]),
        open: Number(r[1]),
        high: Number(r[2]),
        low: Number(r[3]),
        close: Number(r[4]),
        volume: Number(r[5]),
        complete: r[8] === "1",
      }))
      .reverse();
  };
  const format = (m: number) => (m >= 60 ? `${m / 60}H` : `${m}m`);
  const first = get(format(intervals[0]), limits[0]);
  const [minute, quarter] = await Promise.all([
    first,
    intervals[0] === intervals[1] ? first : get(format(intervals[1]), limits[1]),
  ]);
  return { source: "OKX", minute, quarter };
}

async function kraken(
  symbol: string,
  intervals: [number, number] = [1, 15],
  limits: [number, number] = [MINUTE_LIMIT, QUARTER_LIMIT],
): Promise<ProviderResult> {
  const pair = `${symbol.replace(/USDT$/, "")}USDT`;
  const get = async (minutes: number, keep: number): Promise<Candle[]> => {
    const body = (await fetchJson(
      `https://api.kraken.com/0/public/OHLC?pair=${pair}&interval=${minutes}`,
    )) as { error?: string[]; result?: Record<string, unknown> };
    if (body.error?.length) throw new Error(body.error.join(", "));
    const key = Object.keys(body.result ?? {}).find((k) => k !== "last");
    const rows = key ? ((body.result as Record<string, unknown>)[key] as unknown[]) : undefined;
    if (!Array.isArray(rows) || rows.length === 0) throw new Error("empty response");
    return rows
      .map((row, index) => {
        const r = row as [number, string, string, string, string, string, string];
        return {
          time: Number(r[0]) * 1000,
          open: Number(r[1]),
          high: Number(r[2]),
          low: Number(r[3]),
          volume: Number(r[6]),
          close: Number(r[4]),
          complete: index < rows.length - 1,
        };
      })
      .slice(-keep);
  };
  const first = get(intervals[0], limits[0]);
  const [minute, quarter] = await Promise.all([
    first,
    intervals[0] === intervals[1] ? first : get(intervals[1], limits[1]),
  ]);
  return { source: "Kraken", minute, quarter };
}

const PROVIDERS = [
  { name: "Binance", load: binance },
  { name: "OKX", load: okx },
  { name: "Kraken", load: kraken },
];

export async function loadTACandles(
  symbol: string,
  minutes: number,
  validate: (candles: Candle[]) => void,
  source?: string,
) {
  const errors: string[] = [];
  for (const provider of PROVIDERS.filter((p) => !source || p.name === source)) {
    try {
      const result = await provider.load(symbol, [minutes, minutes], [250, 250]);
      validate(result.minute);
      return { source: result.source, candles: result.minute };
    } catch (error) {
      errors.push(`${provider.name}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  throw new Error(errors.join(" | ") || "Unknown data source");
}

export type ProviderOutcome =
  { ok: true; result: ProviderResult } | { ok: false; errors: string[] };

export async function loadCandles(
  symbol: string,
  validate?: (result: ProviderResult) => void,
): Promise<ProviderOutcome> {
  const errors: string[] = [];
  for (const provider of PROVIDERS) {
    try {
      const result = await provider.load(symbol);
      if (result.minute.length >= 2 && result.quarter.length >= 2) {
        validate?.(result);
        return { ok: true, result };
      }
      errors.push(`${provider.name}: insufficient data`);
    } catch (error) {
      errors.push(`${provider.name}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  return { ok: false, errors };
}
