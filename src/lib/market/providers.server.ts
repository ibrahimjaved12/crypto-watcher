/**
 * Public exchange data providers. Server-only.
 *
 * No API keys are required for these public market endpoints. Providers are
 * tried in order; if every provider fails (for example because the hosting
 * region is blocked) we report the failure instead of inventing prices.
 */

export type Candle = { time: number; close: number; complete?: boolean };

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

async function binance(symbol: string): Promise<ProviderResult> {
  const get = async (interval: string, limit: number): Promise<Candle[]> => {
    const raw = (await fetchJson(
      `https://api.binance.com/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`,
    )) as unknown[];
    if (!Array.isArray(raw) || raw.length === 0) throw new Error("empty response");
    return raw.map((row) => {
      const r = row as [number, string, string, string, string, string, number];
      const duration = interval === "1m" ? 60_000 : 15 * 60_000;
      if (Number(r[6]) !== Number(r[0]) + duration - 1) {
        throw new Error("Invalid candle close timestamp");
      }
      return { time: Number(r[0]), close: Number(r[4]), complete: Number(r[6]) < Date.now() };
    });
  };
  const [minute, quarter] = await Promise.all([get("1m", MINUTE_LIMIT), get("15m", QUARTER_LIMIT)]);
  return { source: "Binance", minute, quarter };
}

async function okx(symbol: string): Promise<ProviderResult> {
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
      .map((r) => ({ time: Number(r[0]), close: Number(r[4]), complete: r[8] === "1" }))
      .reverse();
  };
  const [minute, quarter] = await Promise.all([get("1m", MINUTE_LIMIT), get("15m", QUARTER_LIMIT)]);
  return { source: "OKX", minute, quarter };
}

async function kraken(symbol: string): Promise<ProviderResult> {
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
        const r = row as [number, string, string, string, string];
        return {
          time: Number(r[0]) * 1000,
          close: Number(r[4]),
          complete: index < rows.length - 1,
        };
      })
      .slice(-keep);
  };
  const [minute, quarter] = await Promise.all([get(1, MINUTE_LIMIT), get(15, QUARTER_LIMIT)]);
  return { source: "Kraken", minute, quarter };
}

const PROVIDERS: Array<{ name: string; load: (symbol: string) => Promise<ProviderResult> }> = [
  { name: "Binance", load: binance },
  { name: "OKX", load: okx },
  { name: "Kraken", load: kraken },
];

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
